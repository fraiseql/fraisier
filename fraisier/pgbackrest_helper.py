"""Root-privileged pgBackRest helper via Unix socket (socket-activated) (#424).

Refreshing a staging cluster from a pgBackRest backup means stopping a PostgreSQL
cluster, rewriting its data directory as the cluster's owner, and starting it
again.  The deploy units that need that are ``NoNewPrivileges``, so ``sudo`` is
unavailable to them and the work happens here, behind a socket, as root.

One helper serves **one** ``(fraise, environment)``, and what it may touch is
baked into its root-owned unit file at scaffold time::

    python -I -m fraisier.pgbackrest_helper --deploy-user deployer --stanza main \\
        --repo 1 --cluster 18/staging --target latest --timeout 21600

Its authority is fixed when its unit is installed — visible in ``systemctl cat``,
and not decided per request or read from ``fraises.yaml`` at run time.  Only an
operator installs that unit (#433): a deploy whose render changes it stops as
pending until ``sudo fraisier scaffold-install`` runs.  What the helper adds *on
its own authority*, reading only root-owned state, is what protects against a
wrong or hostile ``cluster:``: it refuses any cluster whose configuration archives
into its stanza — the stanza's source, in practice production — and any cluster
that is root-owned, that is running, or whose data directory is not a directory.

The wire protocol (``fraisier.pgbackrest_protocol``) names an operation and nothing
else.  The data directory is read from ``pg_lsclusters`` for the configured
cluster; pgBackRest runs as that cluster's owner; ``pg_ctlcluster`` runs as root
because starting and stopping a cluster needs it.  A running cluster is never
restored over.

Actions: ``info``, ``status``, ``stop``, ``restore`` (takes the backup ``set`` the
client validated), ``start``.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import pwd
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from fraisier._peer_creds import check_peer_creds, extract_deploy_uid
from fraisier.config.restore_source import (
    CLUSTER_RE,
    LATEST,
    MIN_TIMEOUT_SECONDS,
    NAME_RE,
    TARGET_INSTANT_RE,
)
from fraisier.helper_version import VersionWatch, serve_until_stale
from fraisier.pgbackrest_protocol import (
    MAX_REQUEST_BYTES,
    ClusterRow,
    Request,
    RequestRejected,
    parse_lsclusters,
    parse_request,
    parse_restore_log,
    render_response,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

_PGBACKREST = "/usr/bin/pgbackrest"
_PG_CTLCLUSTER = "/usr/bin/pg_ctlcluster"
_PG_LSCLUSTERS = "/usr/bin/pg_lsclusters"

#: Clusters stop in seconds; a stuck stop is a failure, not something to outwait.
_STOP_TIMEOUT_SECONDS = 900
_QUICK_TIMEOUT_SECONDS = 60
#: How long a peer may take, **in total**, to send its one request line.  The helper
#: serves one connection at a time, so a peer that connects and says nothing — or
#: sends a byte every few seconds — must not hold it.
_READ_TIMEOUT_SECONDS = 30.0
#: Where Debian's ``postgresql-common`` keeps each cluster's configuration.
_ETC_POSTGRESQL = Path("/etc/postgresql")
#: Lines of a failed restore's output handed back for the operator to read.
_TAIL_LINES = 30
#: Files whose presence means PostgreSQL will (re)enter recovery on its next start.
_SIGNAL_FILES = ("recovery.signal", "standby.signal")


@dataclass(frozen=True)
class HelperConfig:
    """What this helper may touch — baked into its unit, never taken from a request."""

    stanza: str
    repo: int
    version: str
    name: str
    target: str
    timeout_seconds: int

    @property
    def cluster(self) -> str:
        return f"{self.version}/{self.name}"


class _Run(Protocol):
    def __call__(
        self, argv: list[str], *, user: str | None, timeout: int
    ) -> subprocess.CompletedProcess[str]: ...


def _run_command(
    argv: list[str], *, user: str | None, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run *argv* — as *user* when given, else as the helper's own (root) account.

    A minimal environment and ``/`` as the working directory: nothing the deploy
    user's session left behind reaches a root-launched process.
    """
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    extra: dict[str, Any] = {}
    if user is not None:
        try:
            entry = pwd.getpwnam(user)
        except KeyError as exc:
            raise HelperError(f"cluster owner {user!r} is not a known user") from exc
        env["HOME"] = entry.pw_dir
        # `user=` alone would leave the child's group as root's.
        extra = {"user": entry.pw_uid, "group": entry.pw_gid, "extra_groups": []}
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
        cwd="/",
        **extra,
    )


class HelperError(Exception):
    """An operation this helper refuses or could not complete."""


class Operations:
    """The five operations, over an injectable command runner."""

    def __init__(
        self,
        config: HelperConfig,
        run: _Run = _run_command,
        *,
        is_dir: Callable[[str], bool] = os.path.isdir,
        etc_root: Path = _ETC_POSTGRESQL,
    ) -> None:
        self._config = config
        self._run = run
        self._is_dir = is_dir
        self._etc_root = etc_root

    # -- reading the cluster ------------------------------------------------

    def _cluster(self) -> ClusterRow:
        """The configured cluster as ``pg_lsclusters`` lists it *now*.

        The data directory and owner come from here — a root helper's source of
        truth — and from nowhere a request or the deploy user's config can reach.
        """
        listed = self._run(
            [_PG_LSCLUSTERS, "--no-header"], user=None, timeout=_QUICK_TIMEOUT_SECONDS
        )
        if listed.returncode != 0:
            raise HelperError(f"pg_lsclusters failed: {listed.stderr.strip()}")
        for row in parse_lsclusters(listed.stdout):
            if (row.version, row.name) == (self._config.version, self._config.name):
                if row.owner == "root":
                    raise HelperError(
                        f"cluster {self._config.cluster} is owned by root: "
                        f"pgBackRest would run as root inside its data directory"
                    )
                if not self._is_dir(row.datadir):
                    # `pg_lsclusters` splits on whitespace, so a data directory with
                    # a space in it arrives truncated to a different path.
                    raise HelperError(
                        f"data directory {row.datadir!r} of cluster "
                        f"{self._config.cluster} is not a directory"
                    )
                return row
        raise HelperError(f"cluster {self._config.cluster} not found by pg_lsclusters")

    def _refuse_the_stanzas_source(self, row: ClusterRow) -> None:
        """Refuse a cluster whose configuration archives into this helper's stanza.

        That cluster is the stanza's **producer** — in practice, production — and
        stopping or restoring it is the disaster this helper exists to avoid.  A
        deploy regenerates and installs the scaffold as root from the repository's
        ``fraises.yaml``, so a wrong ``cluster:`` (or a hostile commit) retargets a
        baked helper; this check is on the helper's own authority and reads only
        root-owned configuration, never the request or ``fraises.yaml``.

        Best-effort by construction: a cluster that archives into the stanza through
        a wrapper script that does not name it is not recognised.
        """
        stanza = re.compile(
            rf"--stanza[=\s]+['\"]?{re.escape(self._config.stanza)}(?![A-Za-z0-9_.-])"
        )
        base = self._etc_root / row.version / row.name
        data = Path(row.datadir)
        files = [
            base / "postgresql.conf",
            *sorted((base / "conf.d").glob("*.conf")),
            data / "postgresql.conf",
            data / "postgresql.auto.conf",
        ]
        for path in files:
            try:
                text = path.read_text()
            except OSError:
                continue
            for line in text.splitlines():
                setting = line.strip()
                if setting.startswith("archive_command") and stanza.search(setting):
                    raise HelperError(
                        f"cluster {self._config.cluster} archives into stanza "
                        f"{self._config.stanza!r} ({path}): it is that stanza's "
                        f"source, not a cluster to stop or refresh"
                    )

    def _pgbackrest(self, *args: str) -> list[str]:
        return [
            _PGBACKREST,
            f"--stanza={self._config.stanza}",
            f"--repo={self._config.repo}",
            *args,
        ]

    # -- dispatch -----------------------------------------------------------

    def handle(self, request: Request) -> dict[str, Any]:
        """Execute *request*; every outcome, failures included, is a reply dict."""
        try:
            return getattr(self, f"_do_{request.action}")(request)
        except HelperError as exc:
            return {"ok": False, "error": str(exc)}
        except subprocess.TimeoutExpired as exc:
            logger.error("%s timed out after %ss", exc.cmd, exc.timeout)
            return {"ok": False, "error": f"timed out after {exc.timeout}s: {exc.cmd}"}
        except OSError as exc:
            logger.error("could not run a command: %s", exc)
            return {"ok": False, "error": f"could not run a command: {exc}"}
        except Exception as exc:
            # A root daemon answers; it does not die on a surprise in one request.
            logger.exception("unexpected error handling %s", request.action)
            return {
                "ok": False,
                "error": f"internal error: {type(exc).__name__}: {exc}",
            }

    # -- the operations -----------------------------------------------------

    def _do_info(self, _request: Request) -> dict[str, Any]:
        row = self._cluster()
        result = self._run(
            self._pgbackrest("info", "--output=json"),
            user=row.owner,
            timeout=_QUICK_TIMEOUT_SECONDS,
        )
        # `pgbackrest info` exits 0 for a stanza that does not exist: the verdict
        # is in the JSON's status code, which the client reads.
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def _do_status(self, _request: Request) -> dict[str, Any]:
        row = self._cluster()
        data = Path(row.datadir)
        return {
            "ok": True,
            "status": row.status,
            "online": row.is_online,
            "port": row.port,
            "owner": row.owner,
            "datadir": row.datadir,
            "signals": [name for name in _SIGNAL_FILES if (data / name).exists()],
        }

    def _do_stop(self, _request: Request) -> dict[str, Any]:
        row = self._cluster()
        self._refuse_the_stanzas_source(row)
        if row.is_online:
            stopped = self._run(
                [_PG_CTLCLUSTER, row.version, row.name, "stop", "-m", "fast"],
                user=None,
                timeout=_STOP_TIMEOUT_SECONDS,
            )
            if stopped.returncode != 0:
                logger.error("pg_ctlcluster stop failed: %s", stopped.stderr.strip())
            if self._cluster().is_online:
                return {
                    "ok": False,
                    "error": f"cluster {self._config.cluster} is still running after "
                    f"stop: {stopped.stderr.strip()}",
                }
        return {"ok": True, "status": "down"}

    def _do_restore(self, request: Request) -> dict[str, Any]:
        row = self._cluster()
        self._refuse_the_stanzas_source(row)
        if row.is_online:
            return {
                "ok": False,
                "error": f"cluster {self._config.cluster} is running; a restore only "
                f"ever replaces a stopped cluster's data directory",
            }
        argv = self._pgbackrest(
            f"--pg1-path={row.datadir}",
            "--log-level-console=detail",
            "restore",
            "--delta",
            "--archive-mode=off",
            f"--set={request.set}",
        )
        if self._config.target != LATEST:
            # `--target-action` is invalid without `--type` (measured on 2.59.2),
            # so for `latest` neither is passed: recovery runs to the end of the
            # archive and promotes on its own.
            argv += [
                "--type=time",
                f"--target={self._config.target}",
                "--target-action=promote",
            ]
        logger.info("restoring %s into %s", request.set, row.datadir)
        result = self._run(argv, user=row.owner, timeout=self._config.timeout_seconds)
        output = f"{result.stdout}\n{result.stderr}"
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "summary": dataclasses.asdict(parse_restore_log(output)),
            "tail": "\n".join(output.strip().splitlines()[-_TAIL_LINES:]),
        }

    def _do_start(self, _request: Request) -> dict[str, Any]:
        row = self._cluster()
        started = self._run(
            [_PG_CTLCLUSTER, row.version, row.name, "start"],
            user=None,
            timeout=self._config.timeout_seconds,
        )
        if not self._cluster().is_online:
            return {
                "ok": False,
                "error": f"cluster {self._config.cluster} did not start: "
                f"{started.stderr.strip()}",
            }
        return {"ok": True, "status": "online"}


# ---------------------------------------------------------------------------
# The socket
# ---------------------------------------------------------------------------


def _read_request(conn: socket.socket, *, deadline_seconds: float) -> bytes | None:
    """One request line, or ``None`` if the peer sent none, too much, or too slowly.

    *deadline_seconds* bounds the **whole** read: a timeout per ``recv`` alone lets a
    peer that sends one byte every few seconds hold the single-threaded loop for
    hours.
    """
    deadline = time.monotonic() + deadline_seconds
    buffer = bytearray()
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("request not complete within %.0fs", deadline_seconds)
            return None
        try:
            conn.settimeout(remaining)
            chunk = conn.recv(4096)
        except OSError as exc:
            logger.warning("read error: %s", exc)
            return None
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_REQUEST_BYTES:
            raise RequestRejected(f"request exceeds {MAX_REQUEST_BYTES} bytes")
    return bytes(buffer.split(b"\n", 1)[0]) if buffer.strip() else None


def _send(conn: socket.socket, **fields: Any) -> None:
    try:
        conn.sendall(render_response(**fields))
    except OSError as exc:
        logger.warning("failed to send a response: %s", exc)


def _handle_connection(
    conn: socket.socket,
    operations: Operations,
    *,
    read_timeout: float = _READ_TIMEOUT_SECONDS,
) -> None:
    """Read, validate and execute one request; nothing runs before validation."""
    with conn:
        try:
            raw = _read_request(conn, deadline_seconds=read_timeout)
            conn.settimeout(None)  # the reply may follow a long job
            if raw is None:
                return
            request = parse_request(raw)
        except RequestRejected as exc:
            logger.warning("rejected request: %s", exc)
            _send(conn, ok=False, error=str(exc))
            return
        _send(conn, **operations.handle(request))


def _serve_connection(
    conn: socket.socket, operations: Operations, *, expected_uid: int | None
) -> None:
    """Enforce ``SO_PEERCRED``, then handle one request.

    No transitional fallback, unlike the older helpers: this one is new, so there
    is no unit rendered before the check existed to stay compatible with.
    """
    if expected_uid is None:
        with conn:
            _send(conn, ok=False, error="no --deploy-user configured: refusing")
        return
    try:
        check_peer_creds(conn, expected_uid=expected_uid)
    except PermissionError as exc:
        logger.warning("rejecting connection: %s", exc)
        with conn:
            _send(conn, ok=False, error=f"peer credentials rejected: {exc}")
        return
    _handle_connection(conn, operations)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> tuple[int | None, HelperConfig]:
    """Parse the arguments baked into the unit; exit on anything malformed.

    The scaffold validated these already.  They are checked again here because
    this process runs as root and a hand-edited or stale unit is exactly how an
    unvalidated value would reach it.
    """
    uid, remaining = extract_deploy_uid(list(argv))
    parser = argparse.ArgumentParser(prog="fraisier-pgbackrest-helper")
    parser.add_argument("--stanza", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--target", default=LATEST)
    parser.add_argument("--timeout", default="21600")
    args = parser.parse_args(remaining)

    problems: list[str] = []
    if uid is None:
        problems.append("--deploy-user is required and must name an existing user")
    if not NAME_RE.fullmatch(args.stanza):
        problems.append(f"--stanza {args.stanza!r} is not a plain name")
    if not (args.repo.isdigit() and 1 <= int(args.repo) <= 256):
        problems.append(f"--repo {args.repo!r} is not an integer from 1 to 256")
    if not CLUSTER_RE.fullmatch(args.cluster):
        problems.append(f"--cluster {args.cluster!r} is not <major>/<name>")
    if args.target != LATEST and not TARGET_INSTANT_RE.fullmatch(args.target):
        problems.append(f"--target {args.target!r} is not 'latest' or an instant")
    if not (args.timeout.isdigit() and int(args.timeout) >= MIN_TIMEOUT_SECONDS):
        problems.append(f"--timeout {args.timeout!r} is below {MIN_TIMEOUT_SECONDS}")
    if problems:
        parser.error("; ".join(problems))

    version, name = args.cluster.split("/", 1)
    return uid, HelperConfig(
        stanza=args.stanza,
        repo=int(args.repo),
        version=version,
        name=name,
        target=args.target,
        timeout_seconds=int(args.timeout),
    )


def main() -> None:
    """Entry point for ``fraisier-pgbackrest-helper``.

    The socket is provided by systemd via ``LISTEN_FDS`` (fd 3, ``Accept=no``).
    """
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    uid, config = parse_args(sys.argv[1:])

    if int(os.environ.get("LISTEN_FDS", "0")) < 1:
        logger.error(
            "LISTEN_FDS not set or zero — must be run via systemd socket activation"
        )
        sys.exit(1)

    server_sock = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.setblocking(True)
    logger.info(
        "fraisier-pgbackrest-helper ready: stanza %s, repo %d, cluster %s, target %s",
        config.stanza,
        config.repo,
        config.cluster,
        config.target,
    )

    operations = Operations(config)
    watch = VersionWatch()
    try:
        serve_until_stale(
            server_sock,
            lambda conn: _serve_connection(conn, operations, expected_uid=uid),
            is_stale=watch.is_stale,
        )
    finally:
        server_sock.close()


if __name__ == "__main__":
    main()

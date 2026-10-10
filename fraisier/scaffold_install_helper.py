"""Root-privileged scaffold-install helper via Unix socket (socket-activated).

A deploy that changed ``fraises.yaml`` re-renders the scaffold tree as the
deploy user, then asks this helper to bring the host's units in line with it.
The helper used to run that render's ``install.sh`` as root, which made anyone
who can write the render root (#433). It now reads the render as untrusted
input and applies only what the root policy allows
(:mod:`fraisier.scaffold_apply`): the units an operator approved, rewritten
with content that passes the validator. Sudoers, nginx, sockets, users,
directories and the root helpers are an operator's, and a render that wants
them changed is reported as pending, which stops the deploy.

Protocol
--------
Request (one JSON line + newline)::

    {"action": "install", "deploy_in_flight": true}

Response (one JSON line + newline)::

    {"ok": true, "stdout": "...", "stderr": "...", "returncode": 0,
     "installed": [...], "pending": [...], "refused": {"unit": ["..."]},
     "deferred_restarts": [...], "failed": [...]}

Error response::

    {"ok": false, "error": "..."}
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from fraisier._peer_creds import check_peer_creds, extract_deploy_uid
from fraisier.helper_version import VersionWatch, serve_until_stale
from fraisier.root_policy import (
    POLICY_ROOT,
    RootPolicyError,
    load_policy,
    policy_path,
)
from fraisier.scaffold.artifacts import SYSTEMD_DIR
from fraisier.scaffold_apply import (
    ApplyOutcome,
    SafeTree,
    UnitWriter,
    apply_scaffold,
    read_installed,
)

if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

_ALLOWED_ACTIONS: frozenset[str] = frozenset({"install"})
_SYSTEMCTL = "/usr/bin/systemctl"

#: The fix for a unit that still names an install.sh, or a policy that is
#: missing: both are an operator's one-time step.
OPERATOR_STEP = (
    "run `sudo fraisier scaffold-install` once as an operator: it installs the "
    "root-owned helper code and writes the root policy (#433)"
)

Apply = Callable[[bool], ApplyOutcome]


def _send_error(conn: socket.socket, message: str) -> None:
    """Send an error response to *conn*."""
    _send_response(conn, {"ok": False, "error": message})


def _send_response(conn: socket.socket, response: dict) -> None:
    """Serialise *response* as a JSON line and send it on *conn*."""
    try:
        conn.sendall(json.dumps(response).encode() + b"\n")
    except OSError as exc:
        logger.warning("Failed to send response: %s", exc)


def _read_request(conn: socket.socket) -> bytes:
    buf = bytearray()
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
        if b"\n" in buf:
            return bytes(buf.split(b"\n", 1)[0]) + b"\n"


def outcome_response(outcome: ApplyOutcome) -> dict:
    """The wire form of an apply."""
    summary = (
        f"installed {len(outcome.installed)} unit(s), "
        f"{len(outcome.unchanged)} unchanged"
    )
    if outcome.installed:
        summary += ": " + ", ".join(outcome.installed)
    return {
        "ok": outcome.ok,
        "stdout": summary,
        "stderr": outcome.report(),
        "returncode": 0 if outcome.ok else 1,
        "installed": outcome.installed,
        "pending": outcome.pending,
        "refused": {
            unit: [str(r) for r in refusals]
            for unit, refusals in outcome.refused.items()
        },
        "deferred_restarts": outcome.deferred_restarts,
        "failed": outcome.failed,
    }


def _handle_connection(conn: socket.socket, apply: Apply) -> None:
    """Read one JSON request from *conn*, apply the render, send the outcome."""
    with conn:
        try:
            raw = _read_request(conn)
        except OSError as exc:
            logger.warning("Read error: %s", exc)
            return

        if not raw.strip():
            return

        try:
            request = json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Malformed request: %s", exc)
            _send_error(conn, f"malformed JSON: {exc}")
            return

        action = request.get("action", "") if isinstance(request, dict) else ""
        if action not in _ALLOWED_ACTIONS:
            _send_error(conn, f"action not allowed: {action!r}")
            return

        # Only a literal JSON `true` counts: the payload reaches a root daemon,
        # and all it may do is defer a restart (#349).
        deploy_in_flight = request.get("deploy_in_flight") is True
        try:
            outcome = apply(deploy_in_flight)
        except Exception as exc:
            logger.exception("Scaffold apply failed")
            _send_error(conn, f"scaffold apply failed: {exc}")
            return

        if not outcome.ok:
            logger.error("Scaffold apply did not complete:\n%s", outcome.report())
        _send_response(conn, outcome_response(outcome))


def _systemctl(*args: str) -> bool:
    try:
        result = subprocess.run(
            [_SYSTEMCTL, *args],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("systemctl %s failed: %s", " ".join(args), exc)
        return False
    if result.returncode != 0:
        logger.error(
            "systemctl %s exited %d: %s",
            " ".join(args),
            result.returncode,
            result.stderr.strip(),
        )
    return result.returncode == 0


def short_hostname() -> str:
    """What install.sh's ``hostname -s`` prints."""
    return socket.gethostname().split(".", 1)[0]


def apply_for_project(
    project: str,
    deploy_in_flight: bool,
    *,
    policy_root: Path = POLICY_ROOT,
    systemd_dir: str | Path = SYSTEMD_DIR,
    systemctl: Callable[..., bool] = _systemctl,
    hostname: Callable[[], str] = short_hostname,
) -> ApplyOutcome:
    """Load this project's root policy and apply the render it points at.

    The policy is read on every request, so an operator's scaffold-install
    takes effect without restarting this helper. A policy that is missing, or
    that someone other than root could have written, is not an error to work
    around: nothing is applied, and the deploy is told what the operator must
    do.
    """
    try:
        policy = load_policy(policy_path(project, policy_root))
    except RootPolicyError as exc:
        return ApplyOutcome(pending=[f"{exc}"])
    if policy.project != project:
        return ApplyOutcome(
            pending=[f"the root policy is for {policy.project!r}, not {project!r}"]
        )
    tree = SafeTree(policy.scaffold_dir)
    return apply_scaffold(
        policy,
        read_tree=tree.read,
        read_installed=read_installed,
        write_unit=UnitWriter(systemd_dir).write,
        systemctl=systemctl,
        hostname=hostname(),
        deploy_in_flight=deploy_in_flight,
    )


def _refuse(reason: str) -> Apply:
    def apply(_deploy_in_flight: bool) -> ApplyOutcome:
        return ApplyOutcome(pending=[reason])

    return apply


def _serve_connection(
    conn: socket.socket,
    *,
    expected_uid: int | None,
    apply: Apply,
) -> None:
    """Enforce SO_PEERCRED, then dispatch one request to ``_handle_connection``.

    A root helper with no deploy uid to check against serves nobody.
    """
    if expected_uid is None:
        with conn:
            _send_error(
                conn,
                "peer credentials cannot be checked: the helper unit names no "
                "--deploy-user that exists on this host",
            )
        return
    try:
        check_peer_creds(conn, expected_uid=expected_uid)
    except PermissionError as exc:
        logger.warning("Rejecting connection: %s", exc)
        with conn:
            _send_error(conn, f"peer credentials rejected: {exc}")
        return
    _handle_connection(conn, apply)


def _build_server_socket() -> socket.socket:
    """Acquire socket from systemd socket activation (LISTEN_FDS protocol).

    Raises:
        SystemExit: If LISTEN_FDS is not set or zero.
    """
    listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
    if listen_fds < 1:
        logger.error(
            "LISTEN_FDS not set or zero — must be run via systemd socket activation"
        )
        sys.exit(1)

    # First activated socket is fd 3 (SD_LISTEN_FDS_START = 3)
    server_sock = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.setblocking(True)
    logger.info("fraisier-scaffold-install-helper ready")
    return server_sock


def _pop_project(argv: list[str]) -> tuple[str | None, list[str]]:
    remaining = list(argv)
    if "--project" not in remaining:
        return None, remaining
    i = remaining.index("--project")
    if i + 1 >= len(remaining):
        return None, remaining
    project = remaining[i + 1]
    del remaining[i : i + 2]
    return project, remaining


def build_apply(argv: list[str]) -> Apply:
    """The apply this helper's argv asks for, or a refusal that says why not.

    A unit rendered before #433 passes the path of an ``install.sh`` instead
    of ``--project``. That script is the escalation, so it is never run: every
    request is answered with the operator step instead.
    """
    project, remaining = _pop_project(argv)
    if project is None:
        legacy = f" ({remaining[0]})" if remaining else ""
        return _refuse(
            f"this scaffold-install-helper unit predates the root policy and "
            f"names an install script{legacy} that it will not run as root; "
            + OPERATOR_STEP
        )
    return lambda deploy_in_flight: apply_for_project(project, deploy_in_flight)


def main() -> None:
    """Entry point for fraisier-scaffold-install-helper.

    Argv, baked in at render time::

        fraisier-scaffold-install-helper --deploy-user <name> --project <name>

    The socket file descriptor is provided by systemd via ``LISTEN_FDS``
    (fd 3 = first socket, ``Accept=no``).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    deploy_uid, remaining = extract_deploy_uid(sys.argv[1:])
    apply = build_apply(remaining)
    server_sock = _build_server_socket()

    watch = VersionWatch()
    try:
        serve_until_stale(
            server_sock,
            lambda conn: _serve_connection(conn, expected_uid=deploy_uid, apply=apply),
            is_stale=watch.is_stale,
        )
    finally:
        server_sock.close()


if __name__ == "__main__":
    main()

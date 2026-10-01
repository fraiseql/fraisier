"""The client half of a pgBackRest restore: the helper socket, and which backup (#424).

``HelperClient`` talks to the root helper (``fraisier.pgbackrest_helper``) over its
Unix socket — one action per connection, nothing else on the wire.  ``parse_info``
and ``select_backup`` decide, *before anything is destroyed*, which backup a
refresh will restore, so its label, stop time and age are checked and logged while
the staging cluster is still up.  The helper is then told to restore that exact
label (``--set``), so the backup that was validated is the backup that is restored.

``pgbackrest info`` exits 0 for a stanza that does not exist; the verdict is in the
JSON's ``status.code``, and that is what is read here.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from fraisier.config.restore_source import PgBackRestSpec

log = logging.getLogger(__name__)

#: Seconds to wait for the helper on anything quick (``info``, ``stop``, ``status``).
DEFAULT_TIMEOUT_SECONDS = 120.0

#: Operations that can take as long as the restore itself.  ``stop`` is one: the
#: helper allows a cluster 900s to stop, and a client that gave up sooner would
#: report a failure while the helper went on stopping it.
_LONG_ACTIONS = frozenset({"restore", "start", "stop"})

#: Largest reply the client will read.  Nothing legitimate is near it (an ``info``
#: of a large repository is the biggest); a listener that never ends a line is not
#: one to buffer.
_MAX_REPLY_BYTES = 8 * 1024 * 1024


class BackupChoiceError(RuntimeError):
    """No backup can be chosen: no such stanza, none to restore, none old enough."""


class HelperUnavailableError(RuntimeError):
    """The helper could not be reached, answered nonsense, or refused the request."""

    def __init__(self, message: str, reply: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reply = reply or {}


# ---------------------------------------------------------------------------
# Choosing a backup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Backup:
    """One backup in the repository, as ``pgbackrest info`` reports it."""

    label: str
    kind: str
    stop: datetime
    size_bytes: int


def parse_info(raw: str, *, stanza: str) -> list[Backup]:
    """The restorable backups of *stanza*, oldest first.

    Raises:
        BackupChoiceError: the output is not ``info``'s JSON, the stanza is not in
            it, pgBackRest reported a stanza-level error (exit 0 notwithstanding),
            or it has no backup that finished cleanly.
    """
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupChoiceError(f"pgbackrest info did not return JSON: {exc}") from exc
    entries = payload if isinstance(payload, list) else []
    entry = next(
        (e for e in entries if isinstance(e, dict) and e.get("name") == stanza), None
    )
    if entry is None:
        raise BackupChoiceError(f"pgbackrest info has no stanza {stanza!r}")

    status = entry.get("status") or {}
    if status.get("code", 0) != 0:
        message = status.get("message", "pgBackRest reported an error")
        raise BackupChoiceError(f"stanza {stanza!r}: {message}")

    backups = [
        Backup(
            label=str(b["label"]),
            kind=str(b.get("type", "")),
            stop=datetime.fromtimestamp(int(b["timestamp"]["stop"]), UTC),
            size_bytes=int((b.get("info") or {}).get("size", 0)),
        )
        for b in entry.get("backup") or []
        if not b.get("error")
    ]
    if not backups:
        raise BackupChoiceError(f"stanza {stanza!r} has no backups to restore")
    return sorted(backups, key=lambda b: (b.stop, b.label))


def select_backup(backups: list[Backup], spec: PgBackRestSpec) -> Backup:
    """The backup a refresh to ``spec.target`` restores.

    ``latest`` is the backup that stopped last.  An instant takes the latest backup
    that **stopped before it** — pgBackRest's own rule, since a backup that was
    still running at the target cannot be the base for reaching it.
    """
    if spec.is_latest:
        return backups[-1]
    try:
        target = datetime.fromisoformat(spec.target)
    except ValueError as exc:
        raise BackupChoiceError(
            f"target {spec.target!r} is not an instant: {exc}"
        ) from exc
    eligible = [b for b in backups if b.stop < target]
    if not eligible:
        earliest = backups[0]
        raise BackupChoiceError(
            f"no backup stopped before {spec.target}; the earliest is "
            f"{earliest.label} (stopped {earliest.stop.isoformat()})"
        )
    return eligible[-1]


# ---------------------------------------------------------------------------
# The helper's socket
# ---------------------------------------------------------------------------


class HelperClient:
    """One request, one reply, per connection."""

    def __init__(
        self,
        socket_path: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        long_timeout: float | None = None,
    ) -> None:
        self._path = socket_path
        self._timeout = timeout
        self._long_timeout = long_timeout if long_timeout is not None else timeout

    def timeout_for(self, action: str) -> float:
        return self._long_timeout if action in _LONG_ACTIONS else self._timeout

    def call(self, action: str, **fields: str) -> dict[str, Any]:
        """Send *action* and return the helper's reply.

        Raises:
            HelperUnavailableError: no socket, no answer, an unreadable answer, or
                a reply with ``ok`` false (the helper's own message is the error).
        """
        request = json.dumps({"action": action, **fields}).encode() + b"\n"
        timeout = self.timeout_for(action)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(timeout)
                conn.connect(self._path)
                conn.sendall(request)
                raw = self._read_line(conn)
        except TimeoutError as exc:
            raise HelperUnavailableError(
                f"no answer from the pgBackRest helper at {self._path} within "
                f"{timeout:g}s for {action!r}"
            ) from exc
        except OSError as exc:
            raise HelperUnavailableError(
                f"the pgBackRest helper is not reachable at {self._path}: {exc}. "
                f"It is installed by `fraisier scaffold && sudo fraisier "
                f"scaffold-install --yes`; `fraisier doctor` (pgbackrest_helper) "
                f"says which environments lack it"
            ) from exc

        try:
            reply: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HelperUnavailableError(
                f"unreadable reply from the pgBackRest helper: {raw[:200]!r}"
            ) from exc
        if not isinstance(reply, dict):
            raise HelperUnavailableError(f"unreadable reply from the helper: {reply!r}")
        if not reply.get("ok"):
            detail = (
                reply.get("error")
                or reply.get("tail")
                or reply.get("stderr")
                or f"exit {reply.get('returncode')}"
            )
            raise HelperUnavailableError(
                f"pgBackRest helper refused or failed {action!r}: {detail}", reply
            )
        return reply

    def _read_line(self, conn: socket.socket) -> bytes:
        buffer = bytearray()
        while b"\n" not in buffer:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > _MAX_REPLY_BYTES:
                raise HelperUnavailableError(
                    f"reply from the pgBackRest helper at {self._path} is too large "
                    f"(over {_MAX_REPLY_BYTES} bytes)"
                )
        return bytes(buffer)

    # -- the five operations ------------------------------------------------

    def info(self) -> str:
        return str(self.call("info")["stdout"])

    def status(self) -> dict[str, Any]:
        return self.call("status")

    def stop(self) -> dict[str, Any]:
        return self.call("stop")

    def restore(self, label: str) -> dict[str, Any]:
        return self.call("restore", set=label)

    def start(self) -> dict[str, Any]:
        return self.call("start")


# ---------------------------------------------------------------------------
# The restored cluster, once it is up
# ---------------------------------------------------------------------------

#: Settings a pgBackRest restore leaves behind that a promoted staging cluster must
#: not keep: ``restore_command`` reads production's archive, ``primary_conninfo``
#: would reconnect to a primary, and the recovery targets are a finished recovery's.
#: Measured on 2.59.2: ``postgresql.auto.conf`` keeps all of them after promotion.
_RESET_SETTINGS = (
    "restore_command",
    "primary_conninfo",
    "recovery_target_time",
    "recovery_target_action",
)
#: The two a verified reset must leave empty.
_MUST_BE_EMPTY = ("restore_command", "primary_conninfo")


def _connect(admin_url: str):
    import psycopg

    return psycopg.connect(admin_url, autocommit=True, connect_timeout=10)


def read_setting(admin_url: str, name: str) -> str:
    """``SHOW <name>`` on the restored cluster."""
    from psycopg import sql

    with _connect(admin_url) as conn:
        row = conn.execute(sql.SQL("SHOW {}").format(sql.Identifier(name))).fetchone()
    return "" if row is None else str(row[0])


def wait_until_promoted(
    admin_url: str,
    *,
    timeout_seconds: float,
    interval: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Block until the cluster has finished recovery and accepts writes.

    ``pg_ctlcluster start`` returns when the server accepts connections, which a
    server replaying WAL does before it is promoted.  A refused or "starting up"
    connection is retried: that is a cluster still coming up, not an error.

    Raises:
        TimeoutError: still in recovery (or not answering) at the deadline.
    """
    import psycopg

    deadline = clock() + timeout_seconds
    last = "no connection yet"
    while True:
        try:
            with _connect(admin_url) as conn:
                row = conn.execute("SELECT pg_is_in_recovery()").fetchone()
            if row is not None and row[0] is False:
                return
            last = "still in recovery"
        except psycopg.OperationalError as exc:
            last = str(exc).strip().splitlines()[0] if str(exc).strip() else "refused"
        if clock() >= deadline:
            raise TimeoutError(
                f"the restored cluster did not finish recovery within "
                f"{timeout_seconds:g}s ({last})"
            )
        sleep(interval)


def reset_replication_settings(
    admin_url: str,
    *,
    timeout_seconds: float = 15.0,
    interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, str]:
    """Drop what a restore left in ``postgresql.auto.conf`` and verify it is gone.

    ``ALTER SYSTEM RESET`` plus a reload, then each setting is read back: a reload
    is asynchronous, and "reset" without a read-back is a claim, not a check.
    Returns the values of the settings that must be empty, as read — the caller
    refuses if any is not.
    """
    from psycopg import sql

    with _connect(admin_url) as conn:
        for name in _RESET_SETTINGS:
            conn.execute(sql.SQL("ALTER SYSTEM RESET {}").format(sql.Identifier(name)))
        conn.execute("SELECT pg_reload_conf()")

    deadline = clock() + timeout_seconds
    while True:
        values = {name: read_setting(admin_url, name) for name in _MUST_BE_EMPTY}
        if not any(values.values()) or clock() >= deadline:
            return values
        sleep(interval)


def database_exists(admin_url: str, db_name: str) -> bool:
    """Does the restored cluster hold a database called *db_name*?"""
    with _connect(admin_url) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
        ).fetchone()
    return row is not None

"""Deployment status file — state machine readable by monitoring and CI.

Provides atomic write/read of deployment state as JSON files.
Uses temp file + rename for POSIX atomicity (no partial reads).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from fraisier.errors import ValidationError

logger = logging.getLogger("fraisier")

DEFAULT_STATUS_DIR = Path("/var/lib/fraisier/status")

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _validate_fraise_name(name: str) -> None:
    """Reject fraise names that could cause path traversal."""
    if not _SAFE_NAME_RE.match(name):
        msg = f"Invalid fraise name: {name!r} — must match [a-zA-Z0-9_-]+"
        raise ValidationError(msg)


#: States meaning the deploy did not ship. Branch on membership here rather
#: than ``state == "failed"``: a consumer that tests equality reports
#: ``rollback_failed`` — the state where the schema may be half-migrated — as
#: though nothing were wrong (#293).
FAILURE_STATES = frozenset({"failed", "rolled_back", "rollback_failed", "interrupted"})

#: States a deploy passes *through*. A record left in one of these has either a
#: live owner or none at all — see :func:`reconcile_orphaned_deploys`.
NON_TERMINAL_STATES = frozenset({"pending", "deploying"})

_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


@dataclass
class DeploymentStatusFile:
    """Deployment status as a state machine.

    States: idle -> pending -> deploying -> success | failed | rolled_back
    | rollback_failed | interrupted

    ``rolled_back`` means the deploy failed and the previous state was restored;
    ``rollback_failed`` means the restore itself failed and the database schema
    may be half-migrated — the service must not be restarted until an operator
    has resolved it. Consumers that branch on this field must handle both, or
    they will silently mis-report a dirty schema.

    ``interrupted`` is deliberately *not* ``failed``: the deploy's own failure
    path never ran. Nothing rolled back, ``version.json`` was not restored, and
    the tree may be half-deployed. It is written by
    :func:`reconcile_orphaned_deploys`, never by a deploy — a deploy that could
    write it would not have been interrupted.

    The four ``owner_*`` fields exist so a reader can tell a deploy that is
    running from one whose process is gone. Without them a SIGKILLed deploy sits
    at ``deploying`` for good, which is exactly how it looks while it works.
    ``owner_invocation_id`` is not used to decide liveness; it is recorded so an
    operator can pull the deploy's own journal back out afterwards with
    ``journalctl _SYSTEMD_INVOCATION_ID=<id>``.
    """

    fraise_name: str
    environment: str
    state: str = "idle"
    version: str | None = None
    commit_sha: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None
    migration_report: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    owner_pid: int | None = None
    owner_boot_id: str | None = None
    owner_invocation_id: str | None = None


def _read_boot_id() -> str | None:
    """This boot's kernel-assigned id, or None where there is no such file."""
    try:
        return _BOOT_ID_PATH.read_text().strip() or None
    except OSError:
        return None


def current_owner() -> dict[str, Any]:
    """Identity fields for the process about to claim a deployment record."""
    return {
        "owner_pid": os.getpid(),
        "owner_boot_id": _read_boot_id(),
        "owner_invocation_id": os.environ.get("INVOCATION_ID") or None,
    }


def _pid_alive(pid: int) -> bool:
    """True when a process with *pid* exists (any owner)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists and belongs to someone else.
        return True
    except OSError:
        return True
    return True


def owner_is_gone(status: DeploymentStatusFile) -> bool:
    """True only when the process that wrote *status* provably no longer exists.

    Two definitive answers and one deliberate abstention:

    - a different boot id means the machine restarted, so nothing from that boot
      is running;
    - no process with that pid means the owner is gone;
    - anything else — including a record written before this release, which
      carries no identity at all — returns False.

    The abstention is the safe direction. Declaring a live deploy dead would
    overwrite the record it is still going to write; failing to reconcile a dead
    one leaves the pre-existing behaviour, which ``fraisier doctor`` and the
    elapsed time already make visible.
    """
    boot_id = _read_boot_id()
    if status.owner_boot_id and boot_id and status.owner_boot_id != boot_id:
        return True
    if status.owner_pid is None:
        return False
    return not _pid_alive(status.owner_pid)


def write_status(
    status: DeploymentStatusFile,
    status_dir: Path = DEFAULT_STATUS_DIR,
) -> Path:
    """Write deployment status atomically (temp file + rename).

    Args:
        status: The deployment status to write.
        status_dir: Directory for status files.

    Returns:
        Path to the written status file.
    """
    _validate_fraise_name(status.fraise_name)
    status_dir.mkdir(parents=True, exist_ok=True)
    path = status_dir / f"{status.fraise_name}.status.json"

    # Write to temp file in same directory, then atomic rename.
    # mkstemp() returns an OS fd; close it immediately so write_text() can
    # open the path independently.  The fd must be closed even on error.
    _fd, tmp_path_str = tempfile.mkstemp(
        dir=status_dir, suffix=".tmp", prefix=f"{status.fraise_name}."
    )
    os.close(_fd)
    tmp_path = Path(tmp_path_str)
    try:
        tmp_path.write_text(json.dumps(asdict(status), indent=2))
        tmp_path.rename(path)  # Atomic on POSIX
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return path


def read_status(
    fraise_name: str,
    status_dir: Path = DEFAULT_STATUS_DIR,
) -> DeploymentStatusFile | None:
    """Read deployment status from file.

    Args:
        fraise_name: Name of the fraise.
        status_dir: Directory for status files.

    Returns:
        DeploymentStatusFile or None if no status file exists.
    """
    _validate_fraise_name(fraise_name)
    path = status_dir / f"{fraise_name}.status.json"
    if not path.exists():
        return None

    return _from_dict(json.loads(path.read_text()))


def _from_dict(data: dict[str, Any]) -> DeploymentStatusFile:
    """Build a status from JSON, ignoring fields this version does not know.

    A self-upgrade puts two fraisier versions on one host by design, so a file
    written by the newer one is read by the older until it restarts. Passing the
    raw dict straight into the dataclass turns that into a ``TypeError`` and
    takes ``fraisier status`` down with it.
    """
    known = {f.name for f in fields(DeploymentStatusFile)}
    return DeploymentStatusFile(**{k: v for k, v in data.items() if k in known})


def elapsed_seconds(status: DeploymentStatusFile) -> float | None:
    """Compute elapsed seconds since deployment started.

    Args:
        status: The deployment status file.

    Returns:
        Seconds elapsed since started_at, or None if not started or invalid.
    """
    if not status.started_at:
        return None

    try:
        # Parse ISO 8601 timestamp (assume UTC)
        import datetime

        if status.started_at.endswith("Z"):
            started_dt = datetime.datetime.fromisoformat(
                status.started_at[:-1]
            ).replace(tzinfo=datetime.UTC)
        else:
            started_dt = datetime.datetime.fromisoformat(status.started_at)
        started_ts = started_dt.timestamp()
        return time.time() - started_ts
    except (ValueError, TypeError, AttributeError):
        return None


def reconcile_orphaned_deploys(
    status_dir: Path = DEFAULT_STATUS_DIR,
) -> list[str]:
    """Give a terminal record to every deploy whose process is provably gone.

    A deploy killed mid-flight cannot report: that is the whole point of #349,
    where restarting the webhook from inside its own deploy left the record at
    ``deploying`` for good while the kernel quietly released the flock. So the
    record is closed by whoever comes next — this runs at webhook startup, and
    the restart that killed the deploy is itself what brings it up.

    Returns the fraise names that were reconciled. A file that cannot be read or
    written is skipped: one bad record must not stop the others being closed,
    and must not stop the webhook from starting.
    """
    if not status_dir.exists():
        return []

    reconciled: list[str] = []
    for path in sorted(status_dir.glob("*.status.json")):
        try:
            status = _from_dict(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            logger.warning("Skipping unreadable status file %s", path)
            continue
        if status.state not in NON_TERMINAL_STATES or not owner_is_gone(status):
            continue

        status.state = "interrupted"
        status.finished_at = _now_iso()
        status.error_message = (
            f"The deploy did not report: the process that started it "
            f"(pid {status.owner_pid}) no longer exists. It was terminated "
            f"rather than failing, so nothing rolled back and the tree may be "
            f"half-deployed — check before redeploying."
            + (
                f" Its journal: journalctl _SYSTEMD_INVOCATION_ID="
                f"{status.owner_invocation_id}"
                if status.owner_invocation_id
                else ""
            )
        )
        try:
            write_status(status, status_dir=status_dir)
        except (OSError, ValidationError):
            logger.warning("Could not reconcile status file %s", path, exc_info=True)
            continue
        logger.warning(
            "Deployment record for %s was left at %r by a process that is gone; "
            "recorded as interrupted",
            status.fraise_name,
            "deploying",
        )
        reconciled.append(status.fraise_name)
    return reconciled


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(tz=datetime.UTC).isoformat()

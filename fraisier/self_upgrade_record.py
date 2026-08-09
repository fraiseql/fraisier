"""The record a failed self-upgrade leaves behind (#351).

``maybe_self_upgrade`` detaches a worker that runs
``uv tool install --force fraisier==X`` against the webhook user's own uv tool
dir. ``--force`` removes before it verifies, so *any* mid-install failure — a
root-owned ``__pycache__`` it cannot delete, a full disk, a network error, a
killed worker — can leave the venv half-removed: ``bin/`` gone, ``lib/`` intact,
and every ``~/.local/bin/fraisier*`` symlink dangling.

The running webhook survives that, because a live process outlives its deleted
binary. Nothing looks wrong until the next restart fails 203/EXEC — and on a
deploy host the next restart is often the thing you are relying on to fix
something else.

Until now the only trace was a file under ``/var/lib/fraisier/self-upgrade/``
that nothing reads. This ledger is what ``fraisier doctor`` reports instead, in
the shape v0.63.0 gave deferred restarts: **the entry is cleared only when a
later upgrade succeeds**, so a debt nobody paid stays on the books rather than
looking settled.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

#: Lives beside ``.draining`` and ``.deferred-restarts`` in the lock dir, and is
#: dot-prefixed for the same reason: ``count_held_deployment_locks`` globs
#: ``*.lock`` there and a stray match changes the answer to "is a deploy in
#: flight".
SELF_UPGRADE_FAILURE_FILE = ".self-upgrade-failure"

#: uv's stderr is unbounded and this file sits in a runtime directory, so the
#: detail is truncated. The head is what carries the cause; the full text stays
#: in the worker's own log under /var/lib/fraisier/self-upgrade/.
_MAX_DETAIL = 4000


@dataclass(frozen=True)
class SelfUpgradeFailure:
    """An upgrade that ran and did not land."""

    required: str
    installed: str
    rc: int
    detail: str
    recorded_at: str


def record_self_upgrade_failure(
    lock_dir: Path | str,
    *,
    required: str,
    installed: str,
    rc: int,
    detail: str,
) -> None:
    """Write the failure record, replacing any previous one.

    Best-effort and never raises: this runs on the path where an upgrade has
    *already* failed, and an error here must not mask the error it exists to
    report.
    """
    path = Path(lock_dir) / SELF_UPGRADE_FAILURE_FILE
    payload = {
        "required": required,
        "installed": installed,
        "rc": rc,
        "detail": (detail or "")[:_MAX_DETAIL],
        "recorded_at": datetime.now(tz=UTC).isoformat(),
    }
    try:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    except OSError as exc:
        log.warning("could not record the self-upgrade failure at %s: %s", path, exc)


def read_self_upgrade_failure(lock_dir: Path | str) -> SelfUpgradeFailure | None:
    """The recorded failure, or None when the last upgrade landed.

    A record written by a *newer* fraisier may carry keys this version does not
    know — a self-upgrade puts two versions on one host by design — so unknown
    keys are ignored rather than raising, the same tolerance ``read_status``
    gained in v0.63.0.
    """
    path = Path(lock_dir) / SELF_UPGRADE_FAILURE_FILE
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("could not read the self-upgrade record at %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        # A half-written record must not take `doctor` down with it.
        log.warning("self-upgrade record at %s is not valid JSON; ignoring", path)
        return None
    if not isinstance(data, dict):
        return None
    try:
        return SelfUpgradeFailure(
            required=str(data.get("required", "")),
            installed=str(data.get("installed", "")),
            rc=int(data.get("rc", 0)),
            detail=str(data.get("detail", "")),
            recorded_at=str(data.get("recorded_at", "")),
        )
    except (TypeError, ValueError):
        return None


def clear_self_upgrade_failure(lock_dir: Path | str) -> None:
    """Drop the record. Called only where an upgrade is known to have landed."""
    path = Path(lock_dir) / SELF_UPGRADE_FAILURE_FILE
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not clear the self-upgrade record at %s: %s", path, exc)

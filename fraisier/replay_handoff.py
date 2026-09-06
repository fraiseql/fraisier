"""The marker an upgrade leaves so the next start knows to replay (#367).

``lifespan`` is the natural place to re-fire a dispatch a self-upgrade refused:
the upgrade *ends* by restarting the webhook, so the new process comes up with
the refused-dispatch ledger already on disk and is the first thing to run after
the event that caused the loss. It is also exactly where a replay is most
dangerous — a webhook restarted for any other reason would fire it too, and
"deploy everything in the ledger" is not something an unrelated restart should
ever mean.

So the handoff is explicit. The upgrade worker writes this file immediately
before it requests the restart; the next start **consumes** it — reads and
removes in one step — and replays only then. No marker, no replay.

Shaped like :mod:`fraisier.self_upgrade_record` and
:mod:`fraisier.refused_dispatch_record`, and consumed rather than merely read
for the reason those two are not: a marker that survived its start would
re-deploy on every restart from then on.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Lives beside ``.draining``, ``.deferred-restarts``, ``.self-upgrade-failure``
#: and ``.refused-dispatches`` in the lock dir, and is dot-prefixed for the same
#: reason: ``count_held_deployment_locks`` globs ``*.lock`` there and a stray
#: match changes the answer to "is a deploy in flight".
REPLAY_HANDOFF_FILE = ".replay-on-start"


@dataclass(frozen=True)
class ReplayHandoff:
    """An upgrade asking the process that replaces it to finish its work."""

    version: str
    service: str
    requested_at: str


def _path(lock_dir: Path | str) -> Path:
    return Path(lock_dir) / REPLAY_HANDOFF_FILE


def record_replay_handoff(lock_dir: Path | str, *, version: str, service: str) -> None:
    """Hand the refused dispatches to whatever starts next.

    Best-effort and never raises: this runs in the upgrade worker immediately
    before the restart request, and must not be able to prevent it. A handoff
    that fails to write costs a replay, which ``doctor`` still reports; a
    handoff that raises would cost the upgrade.
    """
    payload = {
        "version": version,
        "service": service,
        "requested_at": datetime.now(tz=UTC).isoformat(),
    }
    try:
        _path(lock_dir).write_text(json.dumps(payload, indent=2, sort_keys=True))
    except OSError as exc:
        log.warning(
            "could not record the replay handoff at %s: %s — the refused "
            "dispatches stay in the ledger for `fraisier doctor` to report",
            _path(lock_dir),
            exc,
        )


def _handoff_from(raw: Any) -> ReplayHandoff | None:
    """One handoff, or None when it cannot be read.

    Unknown keys are ignored rather than raising: a marker written by a *newer*
    fraisier is normal, since a self-upgrade puts two versions on one host by
    design. Same tolerance ``read_status`` gained in v0.63.0.
    """
    if not isinstance(raw, dict):
        return None
    return ReplayHandoff(
        version=str(raw.get("version", "")),
        service=str(raw.get("service", "")),
        requested_at=str(raw.get("requested_at", "")),
    )


def consume_replay_handoff(lock_dir: Path | str) -> ReplayHandoff | None:
    """Take the handoff, if there is one. Reads and removes in one step.

    Removes the file even when its contents cannot be parsed: a marker left
    behind would be retried by every subsequent start, and an unusable handoff
    retried forever is worse than one lost. The debt itself is in the
    refused-dispatch ledger, which this does not touch.

    Never raises. A webhook must start.
    """
    path = _path(lock_dir)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("could not read the replay handoff at %s: %s", path, exc)
        return None
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not remove the replay handoff at %s: %s", path, exc)

    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("replay handoff at %s is not valid JSON; ignoring", path)
        return None
    return _handoff_from(data)

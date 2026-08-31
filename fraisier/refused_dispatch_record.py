"""The record a refused dispatch leaves behind (#365).

While a self-upgrade installs and drains, the webhook raises a ``.draining``
flag and answers new dispatches with HTTP 503 + ``Retry-After``. The
back-pressure is right. The request being *gone* afterwards is not: nothing in
``fraisier health``, nothing in ``deployment-status``, no file, no row. The
branch simply stayed undeployed and looked like one nobody had pushed — and a
caller that does not special-case 503 records a generic failure, which is
indistinguishable from a deploy that started and failed.

Shaped like :mod:`fraisier.self_upgrade_record`, because the requirement is the
same one #351 settled: **a debt nobody paid must not look settled.** An entry is
cleared only when a later deploy for that ``(fraise, environment)`` *succeeds* —
never on read, never at startup, never by the ``Retry-After`` expiring. Clearing
on a restart would mean the upgrade's own restart erases the record of what it
displaced, which is the exact shape of the bug this closes.

It differs from that module in holding a **list**. "Did the last upgrade land"
has one answer; "which targets are behind" has one per target, and one host
serving two environments having both refused is precisely what was reported.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Lives beside ``.draining``, ``.deferred-restarts`` and
#: ``.self-upgrade-failure`` in the lock dir, and is dot-prefixed for the same
#: reason: ``count_held_deployment_locks`` globs ``*.lock`` there and a stray
#: match changes the answer to "is a deploy in flight".
REFUSED_DISPATCH_FILE = ".refused-dispatches"

#: This file is written from a request path into a runtime directory, so it is
#: bounded. Twenty distinct ``(fraise, environment)`` pairs refused on one
#: webhook host is already far past plausible; the oldest is dropped past it.
_MAX_ENTRIES = 20


@dataclass(frozen=True)
class RefusedDispatch:
    """A deploy that was asked for and never ran."""

    fraise: str
    environment: str
    branch: str
    commit_sha: str
    webhook_id: int
    refused_at: str

    @property
    def target(self) -> tuple[str, str]:
        """The dedup key. Deliberately not the branch: two pushes to different
        branches of one target are still one thing to re-fire — the latest."""
        return (self.fraise, self.environment)


def _path(lock_dir: Path | str) -> Path:
    return Path(lock_dir) / REFUSED_DISPATCH_FILE


def _entry_from(raw: Any) -> RefusedDispatch | None:
    """One entry, or None when it cannot be read.

    Unknown keys are ignored rather than raising: a record written by a *newer*
    fraisier is normal, since a self-upgrade puts two versions on one host by
    design. Same tolerance ``read_status`` gained in v0.63.0.
    """
    if not isinstance(raw, dict):
        return None
    try:
        return RefusedDispatch(
            fraise=str(raw.get("fraise", "")),
            environment=str(raw.get("environment", "")),
            branch=str(raw.get("branch", "")),
            commit_sha=str(raw.get("commit_sha", "")),
            webhook_id=int(raw.get("webhook_id", 0)),
            refused_at=str(raw.get("refused_at", "")),
        )
    except (TypeError, ValueError):
        return None


def _write(path: Path, entries: list[RefusedDispatch]) -> None:
    payload = [
        {
            "fraise": e.fraise,
            "environment": e.environment,
            "branch": e.branch,
            "commit_sha": e.commit_sha,
            "webhook_id": e.webhook_id,
            "refused_at": e.refused_at,
        }
        for e in entries
    ]
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def record_refused_dispatch(
    lock_dir: Path | str,
    *,
    fraise: str,
    environment: str,
    branch: str,
    commit_sha: str,
    webhook_id: int,
) -> None:
    """Note that this target was asked for and refused.

    Deduped by ``(fraise, environment)``, newest wins — the operator needs to
    know a target is behind, not how many times it was told so, and the newest
    entry carries the sha worth deploying.

    Best-effort and never raises. The webhook's job at that moment is to answer
    503; this runs underneath that and must not be able to stop it.
    """
    path = _path(lock_dir)
    entry = RefusedDispatch(
        fraise=fraise,
        environment=environment,
        branch=branch,
        commit_sha=commit_sha,
        webhook_id=webhook_id,
        refused_at=datetime.now(tz=UTC).isoformat(),
    )
    entries = [e for e in read_refused_dispatches(lock_dir) if e.target != entry.target]
    entries.append(entry)
    try:
        _write(path, entries[-_MAX_ENTRIES:])
    except OSError as exc:
        log.warning("could not record the refused dispatch at %s: %s", path, exc)


def read_refused_dispatches(lock_dir: Path | str) -> list[RefusedDispatch]:
    """Every target currently owed a deploy. Empty when nothing is.

    Never raises: ``doctor`` must not be taken down by the file it exists to
    read. A malformed file, an unreadable one, and an absent one all read as
    "nothing recorded" — with a WARNING for the first two, since those are not
    the same fact.
    """
    path = _path(lock_dir)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("could not read the refused-dispatch ledger at %s: %s", path, exc)
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("refused-dispatch ledger at %s is not valid JSON; ignoring", path)
        return []
    if not isinstance(data, list):
        return []
    return [e for e in (_entry_from(item) for item in data) if e is not None]


def clear_refused_dispatch(
    lock_dir: Path | str, *, fraise: str, environment: str
) -> None:
    """Discharge one target's debt. Called only on a deploy that succeeded.

    Not on a *failed* deploy: a deploy that ran and failed is a different fact,
    recorded elsewhere, and does not discharge "a request was dropped".
    """
    path = _path(lock_dir)
    remaining = [
        e
        for e in read_refused_dispatches(lock_dir)
        if e.target != (fraise, environment)
    ]
    try:
        if remaining:
            _write(path, remaining)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not clear the refused dispatch at %s: %s", path, exc)

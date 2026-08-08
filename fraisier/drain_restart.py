"""Restart a unit only once the deploys running inside it have finished.

Restarting a unit that hosts a deploy kills that deploy: the webhook runs its
deploys as in-process background tasks and does not exit while one is running,
so systemd's stop timeout expires and the process is SIGKILLed (#349). The
sequence that avoids it is always the same — raise the ``.draining`` flag so no
new deploy is accepted, wait for the in-flight ones to release their locks, then
send the restart over the systemctl-helper socket.

Extracted from :mod:`fraisier.webhook_self_upgrade`, which needed exactly this
for the self-upgrade restart, so the deferred restarts recorded by ``install.sh``
share one implementation rather than growing a second one that drifts.

Scope, as before: the drain is correct for ``lock_backend=file`` (the default).
On ``lock_backend=database`` hosts no ``*.lock`` file exists, so the drain loop
sees nothing held and proceeds immediately.
"""

from __future__ import annotations

import logging
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fraisier.locking import DRAINING_FLAG_NAME, count_held_deployment_locks
from fraisier.service_managers.systemd import _call_via_socket

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator
    from pathlib import Path

log = logging.getLogger(__name__)

#: Distinct from install-failure rc=1 / restart-RPC-failure rc=1 so operators
#: scanning a per-event log can tell drain-timeout apart at a glance.
DRAIN_TIMEOUT_RC = 2

DEFAULT_DRAIN_TIMEOUT_S = 600
DEFAULT_DRAIN_POLL_S = 1.0
DEFAULT_DRAIN_SETTLE_S = 2.0
DEFAULT_LOCK_DIR = "/run/fraisier"


@dataclass(frozen=True)
class DrainResult:
    drained: bool
    held: list[str]


@contextmanager
def draining_flag(lock_dir: Path) -> Iterator[Path]:
    """Touch ``{lock_dir}/.draining`` on entry; always unlink on exit.

    Only one worker may hold this at a time: the flag is a single file and the
    exit unlinks it unconditionally, so a second concurrent holder would have
    its flag cleared by the first one to finish. Callers coordinate by never
    spawning two workers for one deploy.
    """
    flag = lock_dir / DRAINING_FLAG_NAME
    flag.touch()
    try:
        yield flag
    finally:
        flag.unlink(missing_ok=True)


def held_lock_basenames(lock_dir: Path) -> list[str]:
    """Names of ``*.lock`` files currently present — used for timeout logging."""
    if not lock_dir.exists():
        return []
    return sorted(p.name for p in lock_dir.glob("*.lock"))


def wait_for_deploys_to_drain(
    lock_dir: Path,
    timeout_s: float,
    poll_s: float,
) -> DrainResult:
    """Block until no ``*.lock`` is flock'd, or the deadline expires.

    Returns a :class:`DrainResult` so callers can log which locks are still
    held on timeout (the helper does not parse ``/proc/locks`` for holder
    PIDs — basenames are enough to identify which fraise hung).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        held = count_held_deployment_locks(lock_dir)
        if held == 0:
            return DrainResult(drained=True, held=[])
        if time.monotonic() >= deadline:
            return DrainResult(drained=False, held=held_lock_basenames(lock_dir))
        time.sleep(poll_s)


def send_restart(socket_path: str, service: str) -> int:
    """Send the ``restart`` RPC, returning 0 on success and 1 on failure."""
    try:
        _call_via_socket(socket_path, "restart", service)
    except (ConnectionRefusedError, subprocess.CalledProcessError) as exc:
        log.error("restart RPC failed for %s: %s", service, exc)
        return 1
    return 0

"""Integration test fixtures.

``flock_holder`` spawns a subprocess that takes a real ``fcntl.flock`` on a
named lock file and holds it until a release event fires. Used by the
self-upgrade drain regression test to model an in-flight deploy without
patching the locking primitives.
"""

from __future__ import annotations

import multiprocessing
from typing import TYPE_CHECKING, Any

import pytest

from fraisier.locking import file_deployment_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


def _hold_flock(
    lock_dir: Path,
    name: str,
    ready: Any,
    release: Any,
) -> None:
    with file_deployment_lock(name, lock_dir=lock_dir):
        ready.set()
        release.wait(timeout=30)


@pytest.fixture
def flock_holder(
    tmp_path,
) -> Iterator[Callable[[str, Path], tuple[Any, Any]]]:
    """Yield a callable that holds a flock on ``<lock_dir>/<name>.lock``.

    Usage::

        proc, release = flock_holder("staging", lock_dir=tmp_path)
        # ... lock is held; do stuff ...
        release.set()
        proc.join(timeout=5)
    """
    ctx = multiprocessing.get_context("spawn")
    spawned: list[tuple[Any, Any]] = []

    def _spawn(name: str, lock_dir: Path) -> tuple[Any, Any]:
        ready = ctx.Event()
        release = ctx.Event()
        proc = ctx.Process(target=_hold_flock, args=(lock_dir, name, ready, release))
        proc.start()
        # Event-based readiness — no time.sleep polling.
        if not ready.wait(timeout=10):
            release.set()
            proc.join(timeout=5)
            msg = "flock holder did not signal ready"
            raise RuntimeError(msg)
        spawned.append((proc, release))
        return proc, release

    yield _spawn

    for proc, release in spawned:
        release.set()
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2)

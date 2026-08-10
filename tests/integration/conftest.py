"""Integration test fixtures.

``flock_holder`` spawns a subprocess that takes a real ``fcntl.flock`` on a
named lock file and holds it until a release event fires. Used by the
self-upgrade drain regression test to model an in-flight deploy without
patching the locking primitives.

``socket_dir`` yields the local PostgreSQL server's unix socket directory, and
skips the test when there is no usable server. Shared because more than one
end-to-end restore test needs a real ``pg_dump``/``pg_restore`` round trip and
"is a database reachable here" is one fact, not one per test module.
"""

from __future__ import annotations

import multiprocessing
import shutil
from typing import TYPE_CHECKING, Any

import pytest

from fraisier.locking import file_deployment_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_CANDIDATE_DSNS = (
    "postgresql:///postgres",
    "postgresql:///postgres?host=/run/postgresql",
    "postgresql:///postgres?host=/var/run/postgresql",
    "postgresql:///postgres?host=/tmp",
)


def _discover_socket_dir() -> str | None:
    """Return the server's unix socket directory, or None if unusable.

    None is returned when the client tools are missing, no candidate DSN
    connects, or the connecting role cannot create databases — all of which make
    an end-to-end restore test inapplicable rather than failed.
    """
    psycopg = pytest.importorskip("psycopg")
    if not all(shutil.which(tool) for tool in ("pg_dump", "pg_restore")):
        return None
    for dsn in _CANDIDATE_DSNS:
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                socket_dirs, can_create = conn.execute(
                    "SELECT current_setting('unix_socket_directories'), "
                    "(SELECT rolcreatedb OR rolsuper FROM pg_roles "
                    " WHERE rolname = current_user)"
                ).fetchone()
        except Exception:
            continue
        if not can_create:
            return None
        return socket_dirs.split(",")[0].strip()
    return None


@pytest.fixture
def socket_dir() -> str:
    """The local PostgreSQL socket directory, or skip."""
    resolved = _discover_socket_dir()
    if resolved is None:
        pytest.skip("local PostgreSQL with createdb privilege not available")  # ty: ignore[too-many-positional-arguments]
    return resolved


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

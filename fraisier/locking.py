"""Deployment lock mechanism to prevent concurrent deployments.

Two backends:
- **file**: ``fcntl.flock`` — fast, single-machine only (default).
- **database**: SQLite with WAL — works across machines on shared storage.

This module also exposes ``count_held_deployment_locks`` and the
``.draining`` flag helpers used to coordinate the webhook self-upgrade
restart with in-flight deployments (issue #246, ``lock_backend=file``).
"""

import fcntl
import logging
import os
import socket
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from fraisier.errors import DeploymentLockError

logger = logging.getLogger(__name__)

DEFAULT_LOCK_DIR = Path("/run/fraisier")

# Hidden flag file the self-upgrade worker drops in ``lock_dir`` while it is
# installing + draining. The leading dot keeps it out of the ``*.lock`` glob
# used by ``count_held_deployment_locks``.
DRAINING_FLAG_NAME = ".draining"


@contextmanager
def file_deployment_lock(
    fraise_name: str,
    lock_dir: Path | None = None,
) -> Generator[Path]:
    """Acquire a file-based flock for a fraise deployment.

    This is the local fast path — prevents concurrent deploys on the same
    machine. For multi-server coordination, use DeploymentLock (database-backed).

    Args:
        fraise_name: Name of the fraise to lock
        lock_dir: Directory for lock files. Defaults to /run/fraisier.

    Yields:
        Path to the lock file

    Raises:
        DeploymentLockError: If the lock is already held by another process
    """
    if lock_dir is None:
        lock_dir = DEFAULT_LOCK_DIR

    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{fraise_name}.lock"

    lock_file = lock_path.open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        raise DeploymentLockError(f"Deploy already running for {fraise_name}") from None
    except BaseException:
        lock_file.close()
        raise

    try:
        yield lock_path
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def is_deployment_locked(
    fraise_name: str,
    lock_dir: Path | None = None,
) -> bool:
    """Non-blocking check whether a file-based deployment lock is held.

    Attempts to acquire the lock with LOCK_NB; if it succeeds the lock is
    immediately released and False is returned.  If blocked, returns True.

    Args:
        fraise_name: Name of the fraise to check
        lock_dir: Directory for lock files. Defaults to /run/fraisier.

    Returns:
        True if a deployment is currently locked, False otherwise
    """
    if lock_dir is None:
        lock_dir = DEFAULT_LOCK_DIR

    # Open (or create) the lock file and attempt a non-blocking exclusive
    # flock.  No exists() pre-check: that would introduce a TOCTOU window
    # where the file could be created or deleted between the check and the
    # open, producing a false "not locked" answer.
    lock_path = lock_dir / f"{fraise_name}.lock"
    fd = None
    try:
        fd = lock_path.open("w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except FileNotFoundError:
        # Lock directory doesn't exist yet — no deployment can be running.
        return False
    finally:
        if fd is not None:
            fd.close()


def count_held_deployment_locks(lock_dir: Path) -> int:
    """Count ``*.lock`` files in ``lock_dir`` currently held by another process.

    Non-blocking probe via :func:`fcntl.flock` with ``LOCK_NB``. A successful
    acquisition (the lock was free) is released immediately and does not
    count; a ``BlockingIOError`` (the lock is held) increments the counter.

    Files that disappear mid-scan are ignored: a deploy that has just
    finished is by definition no longer in-flight.

    Returns 0 when ``lock_dir`` does not exist (e.g. ``lock_backend=database``
    hosts that never create per-fraise lock files).
    """
    if not lock_dir.exists():
        return 0
    held = 0
    for path in lock_dir.glob("*.lock"):
        try:
            held += _probe_lock_held(path)
        except FileNotFoundError:
            continue
    return held


def _probe_lock_held(path: Path) -> int:
    """Return 1 if ``path`` is flock'd by another process, else 0."""
    with path.open("w") as fd:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 1
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        return 0


def is_draining(lock_dir: Path) -> bool:
    """Return True iff the ``.draining`` flag exists under ``lock_dir``.

    The flag is set by :mod:`fraisier.webhook_self_upgrade` while a worker
    is installing + draining; the webhook consults it to refuse new deploys
    with HTTP 503 + ``Retry-After`` (see :mod:`fraisier.webhook`).
    """
    return (lock_dir / DRAINING_FLAG_NAME).exists()


def clear_draining_flag(lock_dir: Path) -> None:
    """Remove the ``.draining`` flag if present. Tolerates a missing flag."""
    (lock_dir / DRAINING_FLAG_NAME).unlink(missing_ok=True)


@contextmanager
def deployment_lock(fraise_name: str) -> Generator[None]:
    """Acquire a deployment lock using the configured backend.

    Reads ``lock_backend`` from the deployment config:
    - ``"file"`` (default): local fcntl flock.
    - ``"database"``: SQLite-backed lock via :class:`DatabaseDeploymentLock`.
    """
    from fraisier.config import get_config

    try:
        cfg = get_config().deployment
    except FileNotFoundError:
        # No config file — fall back to file lock
        with file_deployment_lock(fraise_name):
            yield
            return

    if cfg.lock_backend == "database":
        db_lock = DatabaseDeploymentLock(
            db_path=Path(cfg.lock_db_path),
        )
        with db_lock(fraise_name):
            yield
    else:
        with file_deployment_lock(fraise_name, lock_dir=Path(cfg.lock_dir)):
            yield


class DatabaseDeploymentLock:
    """Database-backed deployment lock using SQLite with WAL mode.

    Works across machines when the database file is on shared storage
    (NFS/CIFS).  Stale locks are automatically reclaimed after ``ttl``
    seconds.

    Can be used directly via acquire/release or as a context manager::

        lock = DatabaseDeploymentLock(db_path)
        with lock("myfraise"):
            ...  # deploy
    """

    _CREATE_TABLE = """\
        CREATE TABLE IF NOT EXISTS deployment_locks (
            fraise    TEXT PRIMARY KEY,
            holder    TEXT NOT NULL,
            acquired_at REAL NOT NULL
        )
    """

    def __init__(self, db_path: Path, ttl: int = 600) -> None:
        self.db_path = db_path
        self.ttl = ttl
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(self._CREATE_TABLE)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _holder_id() -> str:
        return f"{socket.gethostname()}:{os.getpid()}"

    def acquire(self, fraise: str) -> bool:
        """Try to acquire a lock for *fraise*.

        Returns True if the lock was acquired, False if already held
        by a non-stale holder.  Stale locks (older than TTL) are
        automatically reclaimed.

        Uses BEGIN IMMEDIATE to take a write lock before the SELECT,
        preventing TOCTOU races where two concurrent callers both see
        no row and both try to INSERT.
        """
        now = time.time()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            # BEGIN IMMEDIATE acquires a write lock upfront, serializing
            # concurrent acquire() calls at the database level.
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute(
                "SELECT acquired_at FROM deployment_locks WHERE fraise = ?",
                (fraise,),
            ).fetchone()

            if row is not None:
                if now - row[0] < self.ttl:
                    conn.rollback()
                    return False
                # Stale — reclaim
                logger.warning("Reclaiming stale lock for %s", fraise)
                conn.execute("DELETE FROM deployment_locks WHERE fraise = ?", (fraise,))

            conn.execute(
                "INSERT INTO deployment_locks (fraise, holder, acquired_at) "
                "VALUES (?, ?, ?)",
                (fraise, self._holder_id(), now),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Another process won the race — this is the safety net.
            conn.rollback()
            return False
        finally:
            conn.close()

    def release(self, fraise: str) -> None:
        """Release the lock for *fraise*."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM deployment_locks WHERE fraise = ?", (fraise,))
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def __call__(self, fraise: str) -> Generator[None]:
        """Context manager: acquire on entry, release on exit."""
        if not self.acquire(fraise):
            raise DeploymentLockError(f"Deploy already running for {fraise}")
        try:
            yield
        finally:
            self.release(fraise)

"""Deployment lock mechanism to prevent concurrent deployments.

Uses file-based fcntl.flock for single-machine deployment locking.
"""

import fcntl
import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from fraisier.errors import DeploymentLockError

logger = logging.getLogger(__name__)

DEFAULT_LOCK_DIR = Path("/run/fraisier")


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

    lock_path = lock_dir / f"{fraise_name}.lock"
    if not lock_path.exists():
        return False

    fd = None
    try:
        fd = lock_path.open("w")
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        if fd is not None:
            fd.close()

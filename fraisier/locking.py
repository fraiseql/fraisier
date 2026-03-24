"""Deployment lock mechanism to prevent concurrent deployments.

Prevents multiple simultaneous deployments to the same service on the same provider,
avoiding conflicts and race conditions.

Provides two lock strategies:
- file_deployment_lock: Local fast path using fcntl.flock (single-machine)
- DeploymentLock: Database-backed locks for distributed (multi-server) locking
"""

import fcntl
import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fraisier.database import get_db
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


class DeploymentLockedError(Exception):
    """Raised when a deployment lock cannot be acquired."""

    def __init__(self, service_name: str, provider_name: str):
        """Initialize exception.

        Args:
            service_name: Name of service being deployed
            provider_name: Name of provider/environment
        """
        self.service_name = service_name
        self.provider_name = provider_name
        super().__init__(
            f"Deployment of {service_name} on {provider_name} is already locked"
        )


class DeploymentLock:
    """Lock to prevent concurrent deployments to the same service.

    Usage:
        with DeploymentLock(service_name, provider_name):
            result = provider.deploy_service(...)

    If the lock cannot be acquired, raises DeploymentLockedError.
    """

    def __init__(self, service_name: str, provider_name: str, timeout: int = 300):
        """Initialize deployment lock.

        Args:
            service_name: Name of service to lock
            provider_name: Name of provider/environment
            timeout: Lock timeout in seconds (default: 5 minutes)
        """
        self.service_name = service_name
        self.provider_name = provider_name
        self.timeout = timeout
        self.lock_key = f"{provider_name}:{service_name}"
        self._is_locked = False

    def acquire(self) -> bool:
        """Try to acquire the lock.

        Returns:
            True if lock was acquired, False if already locked
        """
        try:
            db = get_db()

            # Check if lock already exists
            lock_exists = db.get_deployment_lock(self.service_name, self.provider_name)
            if lock_exists:
                # Check if lock has expired
                expires_at_str = lock_exists.get("expires_at")
                if expires_at_str:
                    expires_at = datetime.fromisoformat(expires_at_str)
                    now = datetime.now(UTC)
                    if now < expires_at:
                        # Lock still valid
                        return False
                    # Lock expired, remove it
                    db.release_deployment_lock(self.service_name, self.provider_name)

            # Acquire new lock
            expires_at = datetime.now(UTC) + timedelta(seconds=self.timeout)
            db.acquire_deployment_lock(
                self.service_name, self.provider_name, expires_at
            )

            self._is_locked = True
            return True

        except Exception:
            logger.warning(
                "Failed to acquire deployment lock for %s:%s",
                self.service_name,
                self.provider_name,
                exc_info=True,
            )
            return False

    def release(self) -> None:
        """Release the lock.

        Safe to call even if lock wasn't acquired.
        """
        if self._is_locked:
            try:
                db = get_db()
                db.release_deployment_lock(self.service_name, self.provider_name)
            except Exception:
                logger.warning(
                    "Failed to release deployment lock for %s:%s",
                    self.service_name,
                    self.provider_name,
                    exc_info=True,
                )
            finally:
                self._is_locked = False

    def __enter__(self) -> "DeploymentLock":
        """Context manager entry.

        Raises:
            DeploymentLockedError: If lock cannot be acquired
        """
        if not self.acquire():
            raise DeploymentLockedError(self.service_name, self.provider_name)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit.

        Always releases lock, even if exception occurred.
        """
        self.release()

    @staticmethod
    def is_locked(service_name: str, provider_name: str) -> bool:
        """Check if a service is currently locked.

        Args:
            service_name: Name of service
            provider_name: Name of provider

        Returns:
            True if service is locked, False otherwise
        """
        try:
            db = get_db()
            lock = db.get_deployment_lock(service_name, provider_name)

            if not lock:
                return False

            # Check if expired
            expires_at_str = lock.get("expires_at")
            if expires_at_str:
                expires_at = datetime.fromisoformat(expires_at_str)
                now = datetime.now(UTC)
                return now < expires_at

            return True

        except Exception:
            logger.warning(
                "Failed to check lock status for %s:%s",
                service_name,
                provider_name,
                exc_info=True,
            )
            return False

    @staticmethod
    def get_lock_info(service_name: str, provider_name: str) -> dict[str, Any] | None:
        """Get information about existing lock.

        Args:
            service_name: Name of service
            provider_name: Name of provider

        Returns:
            Lock info dict or None if no lock exists
        """
        try:
            db = get_db()
            lock = db.get_deployment_lock(service_name, provider_name)

            if not lock:
                return None

            return {
                "service_name": service_name,
                "provider_name": provider_name,
                "locked_at": lock.get("locked_at"),
                "expires_at": lock.get("expires_at"),
            }

        except Exception:
            logger.warning(
                "Failed to get lock info for %s:%s",
                service_name,
                provider_name,
                exc_info=True,
            )
            return None

    @staticmethod
    def clear_lock(service_name: str, provider_name: str) -> bool:
        """Forcefully clear a lock.

        Use with caution - only if you know the deployment is complete.

        Args:
            service_name: Name of service
            provider_name: Name of provider

        Returns:
            True if lock was cleared, False if no lock existed
        """
        try:
            db = get_db()
            db.release_deployment_lock(service_name, provider_name)
            return True
        except Exception:
            logger.warning(
                "Failed to clear lock for %s:%s",
                service_name,
                provider_name,
                exc_info=True,
            )
            return False

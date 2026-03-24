"""Deployment lock management for the Fraisier database."""

from datetime import datetime
from typing import Any


class DeploymentLockStore:
    """Manages deployment locks in the database."""

    def __init__(
        self,
        get_connection: Any,
    ):
        self._get_connection = get_connection

    def acquire_deployment_lock(
        self, service_name: str, provider_name: str, expires_at: str | Any
    ) -> None:
        """Acquire a deployment lock for a service/provider.

        Args:
            service_name: Name of service being deployed
            provider_name: Name of provider/environment
            expires_at: When lock expires (ISO format datetime or datetime object)

        Raises:
            Exception: If lock cannot be acquired (already locked)
        """

        # Convert datetime object to ISO format string if needed
        if hasattr(expires_at, "isoformat"):
            expires_at_str = expires_at.isoformat()
        else:
            expires_at_str = expires_at

        now = datetime.now().isoformat()

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO tb_deployment_lock
                    (service_name, provider_name,
                     locked_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (service_name, provider_name, now, expires_at_str),
            )
            conn.commit()

    def release_deployment_lock(self, service_name: str, provider_name: str) -> None:
        """Release a deployment lock.

        Args:
            service_name: Name of service
            provider_name: Name of provider/environment
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                DELETE FROM tb_deployment_lock
                WHERE service_name=? AND provider_name=?
                """,
                (service_name, provider_name),
            )
            conn.commit()

    def get_deployment_lock(
        self, service_name: str, provider_name: str
    ) -> dict[str, Any] | None:
        """Get lock info if service is locked.

        Args:
            service_name: Name of service
            provider_name: Name of provider/environment

        Returns:
            Lock dict or None if no lock exists
        """
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT pk_deployment_lock, service_name,
                    provider_name, locked_at, expires_at
                FROM tb_deployment_lock
                WHERE service_name=? AND provider_name=?
                """,
                (service_name, provider_name),
            ).fetchone()
            return dict(row) if row else None

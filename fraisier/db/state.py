"""Fraise state management for the Fraisier database."""

from datetime import datetime
from typing import Any


class FraiseStateManager:
    """Manages fraise state records in the database."""

    def __init__(
        self,
        get_connection: Any,
    ):
        self._get_connection = get_connection

    @staticmethod
    def _normalize_state_dict(d: dict[str, Any]) -> dict[str, Any]:
        """Add short key aliases for fraise state dicts."""
        if "fraise_name" in d:
            d["fraise"] = d["fraise_name"]
        if "environment_name" in d:
            d["environment"] = d["environment_name"]
        if "job_name" in d:
            d["job"] = d["job_name"]
        return d

    def get_fraise_state(
        self, fraise: str, environment: str, job: str | None = None
    ) -> dict[str, Any] | None:
        """Get current state of a fraise.

        Args:
            fraise: Fraise name
            environment: Environment name
            job: Optional job name for scheduled deployments

        Returns:
            Fraise state dict or None if not found
        """
        with self._get_connection() as conn:
            if job:
                row = conn.execute(
                    "SELECT * FROM v_fraise_status "
                    "WHERE fraise_name=? AND environment_name=? AND job_name=?",
                    (fraise, environment, job),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM v_fraise_status "
                    "WHERE fraise_name=? AND environment_name=? "
                    "AND job_name IS NULL",
                    (fraise, environment),
                ).fetchone()
            return self._normalize_state_dict(dict(row)) if row else None

    def update_fraise_state(
        self,
        fraise: str,
        environment: str,
        version: str,
        status: str = "healthy",
        job: str | None = None,
        deployed_by: str | None = None,
    ) -> None:
        """Update or insert fraise state with trinity identifiers.

        Creates or updates a fraise state with:
        - pk_fraise_state: Internal key (auto-allocated)
        - id: UUID for cross-database sync
        - identifier: Business key (fraise:environment[:job])

        Args:
            fraise: Fraise name
            environment: Environment name
            version: Current deployed version
            status: Health status (healthy, degraded, down, unknown)
            job: Optional job name for scheduled deployments
            deployed_by: User who triggered deployment
        """
        import uuid

        now = datetime.now().isoformat()
        # Generate trinity identifiers
        state_uuid = str(uuid.uuid4())
        identifier = (
            f"{fraise}:{environment}" if not job else f"{fraise}:{environment}:{job}"
        )

        with self._get_connection() as conn:
            # Check if exists to decide between insert or update
            existing = conn.execute(
                "SELECT pk_fraise_state FROM tb_fraise_state "
                "WHERE fraise_name=? AND environment_name=? "
                "AND job_name IS ?",
                (fraise, environment, job),
            ).fetchone()

            if existing:
                # Update existing
                conn.execute(
                    """
                    UPDATE tb_fraise_state
                    SET current_version = ?,
                        last_deployed_at = ?,
                        last_deployed_by = ?,
                        status = ?,
                        updated_at = ?
                    WHERE fraise_name = ? AND environment_name = ? AND job_name IS ?
                    """,
                    (version, now, deployed_by, status, now, fraise, environment, job),
                )
            else:
                # Insert new
                conn.execute(
                    """
                    INSERT INTO tb_fraise_state
                        (id, identifier, fraise_name, environment_name, job_name,
                         current_version, last_deployed_at, last_deployed_by, status,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state_uuid,
                        identifier,
                        fraise,
                        environment,
                        job,
                        version,
                        now,
                        deployed_by,
                        status,
                        now,
                        now,
                    ),
                )

            conn.commit()

    def get_all_fraise_states(self) -> list[dict[str, Any]]:
        """Get state of all fraises."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM v_fraise_status ORDER BY fraise_name, environment_name"
            ).fetchall()
            return [self._normalize_state_dict(dict(row)) for row in rows]

"""Deployment history management for the Fraisier database."""

from datetime import datetime
from typing import Any


class DeploymentHistoryManager:
    """Manages deployment history records in the database."""

    def __init__(
        self,
        get_connection: Any,
    ):
        self._get_connection = get_connection

    @staticmethod
    def _normalize_deployment_dict(d: dict[str, Any]) -> dict[str, Any]:
        """Add short key aliases for deployment dicts."""
        if "fraise_name" in d:
            d["fraise"] = d["fraise_name"]
        if "environment_name" in d:
            d["environment"] = d["environment_name"]
        if "job_name" in d:
            d["job"] = d["job_name"]
        return d

    def start_deployment(
        self,
        fraise: str,
        environment: str = "default",
        triggered_by: str = "manual",
        triggered_by_user: str | None = None,
        git_branch: str | None = None,
        git_commit: str | None = None,
        old_version: str | None = None,
        job: str | None = None,
    ) -> int:
        """Record start of a deployment with trinity identifiers.

        Creates deployment record with:
        - pk_deployment: Internal key (auto-allocated)
        - id: UUID for cross-database sync
        - identifier: Business key (fraise:environment:timestamp)
        - fk_fraise_state: Reference to fraise state (pk_fraise_state, not id)

        Args:
            fraise: Fraise name
            environment: Environment name
            triggered_by: Trigger source (webhook, manual, scheduled)
            triggered_by_user: User who triggered deployment
            git_branch: Git branch deployed
            git_commit: Git commit hash
            old_version: Previous deployed version
            job: Optional job name

        Returns:
            pk_deployment (INTEGER primary key) for this deployment
        """
        import uuid

        now = datetime.now().isoformat()
        deployment_uuid = str(uuid.uuid4())
        identifier = f"{fraise}:{environment}:{now}"

        with self._get_connection() as conn:
            # Get fk_fraise_state (pk_fraise_state from tb_fraise_state)
            fraise_state = conn.execute(
                "SELECT pk_fraise_state FROM tb_fraise_state "
                "WHERE fraise_name=? AND environment_name=? "
                "AND job_name IS ?",
                (fraise, environment, job),
            ).fetchone()

            if fraise_state:
                fk_fraise_state = fraise_state["pk_fraise_state"]
            else:
                # Auto-create fraise state if it doesn't exist
                state_uuid = str(uuid.uuid4())
                state_identifier = (
                    f"{fraise}:{environment}"
                    if not job
                    else f"{fraise}:{environment}:{job}"
                )
                conn.execute(
                    """
                    INSERT INTO tb_fraise_state
                        (id, identifier, fraise_name, environment_name, job_name,
                         status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'unknown', ?, ?)
                    """,
                    (state_uuid, state_identifier, fraise, environment, job, now, now),
                )
                fk_fraise_state = conn.execute(
                    "SELECT pk_fraise_state FROM tb_fraise_state "
                    "WHERE fraise_name=? AND environment_name=? "
                    "AND job_name IS ?",
                    (fraise, environment, job),
                ).fetchone()["pk_fraise_state"]

            cursor = conn.execute(
                """
                INSERT INTO tb_deployment
                    (id, identifier, fk_fraise_state,
                     fraise_name, environment_name, job_name,
                     started_at, status, triggered_by,
                     triggered_by_user, git_branch, git_commit,
                     old_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    deployment_uuid,
                    identifier,
                    fk_fraise_state,
                    fraise,
                    environment,
                    job,
                    now,
                    triggered_by,
                    triggered_by_user,
                    git_branch,
                    git_commit,
                    old_version,
                    now,
                    now,
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def complete_deployment(
        self,
        deployment_id: int,
        success: bool,
        new_version: str | None = None,
        error_message: str | None = None,
        details: str | None = None,
    ) -> None:
        """Record completion of a deployment (pk_deployment).

        Args:
            deployment_id: pk_deployment (INTEGER primary key)
            success: Whether deployment succeeded
            new_version: New deployed version
            error_message: Error message if failed
            details: JSON details of deployment
        """
        now = datetime.now().isoformat()
        status = "success" if success else "failed"

        with self._get_connection() as conn:
            # Get start time to calculate duration using pk_deployment
            row = conn.execute(
                "SELECT started_at FROM tb_deployment WHERE pk_deployment=?",
                (deployment_id,),
            ).fetchone()

            duration = None
            if row:
                started = datetime.fromisoformat(row["started_at"])
                duration = (datetime.now() - started).total_seconds()

            conn.execute(
                """
                UPDATE tb_deployment
                SET completed_at=?, status=?, new_version=?, duration_seconds=?,
                    error_message=?, details=?, updated_at=?
                WHERE pk_deployment=?
                """,
                (
                    now,
                    status,
                    new_version,
                    duration,
                    error_message,
                    details,
                    now,
                    deployment_id,
                ),
            )
            conn.commit()

    def mark_deployment_rolled_back(self, deployment_id: int) -> None:
        """Mark a deployment as rolled back (pk_deployment).

        Args:
            deployment_id: pk_deployment (INTEGER primary key)
        """
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE tb_deployment SET status='rolled_back', "
                "updated_at=? WHERE pk_deployment=?",
                (now, deployment_id),
            )
            conn.commit()

    def get_deployment(self, deployment_id: int) -> dict[str, Any] | None:
        """Get a specific deployment record (pk_deployment).

        Args:
            deployment_id: pk_deployment (INTEGER primary key)

        Returns:
            Deployment record from v_deployment_history or None
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM v_deployment_history WHERE pk_deployment=?",
                (deployment_id,),
            ).fetchone()
            return self._normalize_deployment_dict(dict(row)) if row else None

    def get_recent_deployments(
        self,
        limit: int = 20,
        fraise: str | None = None,
        environment: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent deployment history with trinity identifiers.

        Args:
            limit: Number of deployments to return
            fraise: Filter by fraise name
            environment: Filter by environment name

        Returns:
            List of deployment records from v_deployment_history
        """
        query = "SELECT * FROM v_deployment_history WHERE 1=1"
        params: list[Any] = []

        if fraise:
            query += " AND fraise_name=?"
            params.append(fraise)
        if environment:
            query += " AND environment_name=?"
            params.append(environment)

        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._normalize_deployment_dict(dict(row)) for row in rows]

    def get_deployment_stats(
        self, fraise: str | None = None, days: int = 30
    ) -> dict[str, Any]:
        """Get deployment statistics with trinity identifiers.

        Args:
            fraise: Filter by fraise name
            days: Number of days to include in statistics

        Returns:
            Dictionary with stats: total, successful, failed, rolled_back, avg_duration
        """
        cutoff = datetime.now().isoformat()[:10]  # Just date part

        with self._get_connection() as conn:
            query = """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as successful,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                    SUM(CASE WHEN status='rolled_back'
                        THEN 1 ELSE 0 END) as rolled_back,
                    AVG(duration_seconds) as avg_duration
                FROM tb_deployment
                WHERE started_at >= date(?, '-' || ? || ' days')
            """
            params: list[Any] = [cutoff, days]

            if fraise:
                query += " AND fraise_name=?"
                params.append(fraise)

            row = conn.execute(query, params).fetchone()
            return dict(row) if row else {}

"""SQLite database for Fraisier deployment state and history.

YAML (fraises.yaml) = Configuration (what fraises exist)
SQLite (fraisier.db) = State & History (what's deployed, what happened)

Follows CQRS pattern with clear separation of write (tb_*) and read (v_*) models.
"""

import os
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fraisier.db.history import DeploymentHistoryManager
from fraisier.db.lock_store import DeploymentLockStore
from fraisier.db.state import FraiseStateManager
from fraisier.db.webhook_store import WebhookEventStore

# Default database location
DEFAULT_DB_PATH = Path("/opt/fraisier/fraisier.db")


def get_db_path() -> Path:
    """Get database path from FRAISIER_DB_PATH env, /opt/fraisier, or package dir."""
    env_path = os.getenv("FRAISIER_DB_PATH")
    if env_path:
        return Path(env_path)
    if DEFAULT_DB_PATH.parent.exists():
        return DEFAULT_DB_PATH
    return Path(__file__).parent.parent / "fraisier.db"


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    """Get database connection with row factory and concurrency pragmas."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def _tables_sql() -> str:
    """SQL for write-side tables (tb_*)."""
    return """
        CREATE TABLE IF NOT EXISTS tb_fraise_state (
            id TEXT NOT NULL UNIQUE,
            identifier TEXT NOT NULL UNIQUE,
            pk_fraise_state INTEGER PRIMARY KEY AUTOINCREMENT,
            fraise_name TEXT NOT NULL,
            environment_name TEXT NOT NULL,
            job_name TEXT,
            current_version TEXT,
            last_deployed_at TEXT,
            last_deployed_by TEXT,
            status TEXT DEFAULT 'unknown',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(fraise_name, environment_name, job_name)
        );

        CREATE TABLE IF NOT EXISTS tb_deployment (
            id TEXT NOT NULL UNIQUE,
            identifier TEXT NOT NULL UNIQUE,
            pk_deployment INTEGER PRIMARY KEY AUTOINCREMENT,
            fk_fraise_state INTEGER REFERENCES tb_fraise_state(pk_fraise_state),
            fraise_name TEXT NOT NULL,
            environment_name TEXT NOT NULL,
            job_name TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_seconds REAL,
            old_version TEXT,
            new_version TEXT,
            status TEXT NOT NULL,
            triggered_by TEXT,
            triggered_by_user TEXT,
            git_commit TEXT,
            git_branch TEXT,
            error_message TEXT,
            details TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tb_webhook_event (
            id TEXT NOT NULL UNIQUE,
            identifier TEXT NOT NULL UNIQUE,
            pk_webhook_event INTEGER PRIMARY KEY AUTOINCREMENT,
            fk_deployment INTEGER REFERENCES tb_deployment(pk_deployment),
            received_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            git_provider TEXT NOT NULL,
            branch_name TEXT,
            commit_sha TEXT,
            sender TEXT,
            payload TEXT,
            processed INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tb_deployment_lock (
            pk_deployment_lock INTEGER PRIMARY KEY AUTOINCREMENT,
            service_name TEXT NOT NULL,
            provider_name TEXT NOT NULL,
            locked_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE(service_name, provider_name)
        );
    """


def _views_sql() -> str:
    """SQL for read-side views (v_*)."""
    return """
        CREATE VIEW IF NOT EXISTS v_fraise_status AS
        SELECT
            fs.pk_fraise_state,
            fs.id,
            fs.identifier,
            fs.fraise_name,
            fs.environment_name,
            fs.job_name,
            fs.current_version,
            fs.status,
            fs.last_deployed_at,
            fs.last_deployed_by,
            (SELECT COUNT(*) FROM tb_deployment d
             WHERE d.fk_fraise_state = fs.pk_fraise_state
               AND d.status = 'success') as successful_deployments,
            (SELECT COUNT(*) FROM tb_deployment d
             WHERE d.fk_fraise_state = fs.pk_fraise_state
               AND d.status = 'failed') as failed_deployments,
            fs.created_at,
            fs.updated_at
        FROM tb_fraise_state fs;

        CREATE VIEW IF NOT EXISTS v_deployment_history AS
        SELECT
            d.pk_deployment,
            d.id,
            d.identifier,
            d.fraise_name,
            d.environment_name,
            d.job_name,
            d.started_at,
            d.completed_at,
            d.duration_seconds,
            d.old_version,
            d.new_version,
            d.status,
            d.triggered_by,
            d.triggered_by_user,
            d.git_commit,
            d.git_branch,
            d.error_message,
            CASE
                WHEN d.old_version != d.new_version THEN 'upgrade'
                WHEN d.old_version = d.new_version THEN 'redeploy'
                ELSE 'unknown'
            END as deployment_type,
            d.created_at,
            d.updated_at
        FROM tb_deployment d
        ORDER BY d.started_at DESC;

        CREATE VIEW IF NOT EXISTS v_webhook_event_history AS
        SELECT
            we.pk_webhook_event,
            we.id,
            we.identifier,
            we.git_provider,
            we.event_type,
            we.branch_name,
            we.commit_sha,
            we.sender,
            we.received_at,
            we.processed,
            we.fk_deployment,
            d.id as deployment_id,
            d.fraise_name,
            d.environment_name,
            we.created_at,
            we.updated_at
        FROM tb_webhook_event we
        LEFT JOIN tb_deployment d ON we.fk_deployment = d.pk_deployment
        ORDER BY we.received_at DESC;
    """


def _indexes_sql() -> str:
    """SQL for all indexes."""
    return """
        CREATE INDEX IF NOT EXISTS idx_fraise_state_name_env
            ON tb_fraise_state(fraise_name, environment_name);
        CREATE INDEX IF NOT EXISTS idx_fraise_state_identifier
            ON tb_fraise_state(identifier);
        CREATE INDEX IF NOT EXISTS idx_fraise_state_id
            ON tb_fraise_state(id);

        CREATE INDEX IF NOT EXISTS idx_deployment_fraise_state_fk
            ON tb_deployment(fk_fraise_state);
        CREATE INDEX IF NOT EXISTS idx_deployment_started_at
            ON tb_deployment(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_deployment_identifier
            ON tb_deployment(identifier);
        CREATE INDEX IF NOT EXISTS idx_deployment_id
            ON tb_deployment(id);
        CREATE INDEX IF NOT EXISTS idx_deployment_status
            ON tb_deployment(status);

        CREATE INDEX IF NOT EXISTS idx_webhook_event_deployment_fk
            ON tb_webhook_event(fk_deployment);
        CREATE INDEX IF NOT EXISTS idx_webhook_event_received_at
            ON tb_webhook_event(received_at DESC);
        CREATE INDEX IF NOT EXISTS idx_webhook_event_identifier
            ON tb_webhook_event(identifier);
        CREATE INDEX IF NOT EXISTS idx_webhook_event_id
            ON tb_webhook_event(id);
        CREATE INDEX IF NOT EXISTS idx_webhook_event_processed
            ON tb_webhook_event(processed);

        CREATE INDEX IF NOT EXISTS idx_deployment_lock_service_provider
            ON tb_deployment_lock(service_name, provider_name);
        CREATE INDEX IF NOT EXISTS idx_deployment_lock_expires_at
            ON tb_deployment_lock(expires_at);
    """


def init_database() -> None:
    """Initialize database schema following trinity pattern."""
    with get_connection() as conn:
        conn.executescript(_tables_sql())
        conn.executescript(_views_sql())
        conn.executescript(_indexes_sql())
        conn.commit()


class FraisierDB:
    """High-level interface for Fraisier database operations.

    Delegates to focused manager classes while preserving the original API.
    """

    def __init__(self):
        """Initialize and ensure schema exists."""
        init_database()
        self._state = FraiseStateManager(get_connection)
        self._history = DeploymentHistoryManager(get_connection)
        self._webhooks = WebhookEventStore(get_connection)
        self._locks = DeploymentLockStore(get_connection)

    # =========================================================================
    # Fraise State
    # =========================================================================

    def get_fraise_state(
        self, fraise: str, environment: str, job: str | None = None
    ) -> dict[str, Any] | None:
        """Get current state of a fraise."""
        return self._state.get_fraise_state(fraise, environment, job)

    def update_fraise_state(
        self,
        fraise: str,
        environment: str,
        version: str,
        status: str = "healthy",
        job: str | None = None,
        deployed_by: str | None = None,
    ) -> None:
        """Update or insert fraise state."""
        self._state.update_fraise_state(
            fraise, environment, version, status, job, deployed_by
        )

    def get_all_fraise_states(self) -> list[dict[str, Any]]:
        """Get state of all fraises."""
        return self._state.get_all_fraise_states()

    # =========================================================================
    # Deployment History
    # =========================================================================

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
        """Record start of a deployment."""
        return self._history.start_deployment(
            fraise,
            environment,
            triggered_by,
            triggered_by_user,
            git_branch,
            git_commit,
            old_version,
            job,
        )

    def complete_deployment(
        self,
        deployment_id: int,
        success: bool,
        new_version: str | None = None,
        error_message: str | None = None,
        details: str | None = None,
    ) -> None:
        """Record completion of a deployment."""
        self._history.complete_deployment(
            deployment_id, success, new_version, error_message, details
        )

    def mark_deployment_rolled_back(self, deployment_id: int) -> None:
        """Mark a deployment as rolled back."""
        self._history.mark_deployment_rolled_back(deployment_id)

    def get_deployment(self, deployment_id: int) -> dict[str, Any] | None:
        """Get a specific deployment record."""
        return self._history.get_deployment(deployment_id)

    def get_recent_deployments(
        self,
        limit: int = 20,
        fraise: str | None = None,
        environment: str | None = None,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get recent deployment history."""
        return self._history.get_recent_deployments(limit, fraise, environment, since)

    def get_deployment_stats(
        self, fraise: str | None = None, days: int = 30
    ) -> dict[str, Any]:
        """Get deployment statistics."""
        return self._history.get_deployment_stats(fraise, days)

    # =========================================================================
    # Webhook Events
    # =========================================================================

    def record_webhook_event(
        self,
        event_type: str,
        payload: str,
        branch: str | None = None,
        commit_sha: str | None = None,
        sender: str | None = None,
        git_provider: str = "unknown",
    ) -> int:
        """Record a received webhook event."""
        return self._webhooks.record_webhook_event(
            event_type, payload, branch, commit_sha, sender, git_provider
        )

    def link_webhook_to_deployment(self, webhook_id: int, deployment_id: int) -> None:
        """Link a webhook event to its triggered deployment."""
        self._webhooks.link_webhook_to_deployment(webhook_id, deployment_id)

    def get_recent_webhooks(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get recent webhook events."""
        return self._webhooks.get_recent_webhooks(limit)

    # =========================================================================
    # Deployment Locks
    # =========================================================================

    def acquire_deployment_lock(
        self, service_name: str, provider_name: str, expires_at: str | Any
    ) -> None:
        """Acquire a deployment lock for a service/provider."""
        self._locks.acquire_deployment_lock(service_name, provider_name, expires_at)

    def release_deployment_lock(self, service_name: str, provider_name: str) -> None:
        """Release a deployment lock."""
        self._locks.release_deployment_lock(service_name, provider_name)

    def get_deployment_lock(
        self, service_name: str, provider_name: str
    ) -> dict[str, Any] | None:
        """Get lock info if service is locked."""
        return self._locks.get_deployment_lock(service_name, provider_name)


# Global instance (thread-safe)
_db: FraisierDB | None = None
_db_lock = threading.Lock()


def get_db() -> FraisierDB:
    """Get or create global database instance."""
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = FraisierDB()
    return _db

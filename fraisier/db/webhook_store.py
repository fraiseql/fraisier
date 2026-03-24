"""Webhook event storage for the Fraisier database."""

from datetime import datetime
from typing import Any


class WebhookEventStore:
    """Manages webhook event records in the database."""

    def __init__(
        self,
        get_connection: Any,
    ):
        self._get_connection = get_connection

    @staticmethod
    def _normalize_webhook_dict(d: dict[str, Any]) -> dict[str, Any]:
        """Add short key aliases for webhook dicts."""
        if "pk_webhook_event" in d:
            d["id"] = d["pk_webhook_event"]
        if "branch_name" in d:
            d["branch"] = d["branch_name"]
        if "git_provider" in d:
            d["provider"] = d["git_provider"]
        # deployment_id from the view is the UUID; tests expect pk
        if "fk_deployment" in d:
            d["deployment_id"] = d["fk_deployment"]
        return d

    def record_webhook_event(
        self,
        event_type: str,
        payload: str,
        branch: str | None = None,
        commit_sha: str | None = None,
        sender: str | None = None,
        git_provider: str = "unknown",
    ) -> int:
        """Record a received webhook event with trinity identifiers.

        Creates webhook record with:
        - pk_webhook_event: Internal key (auto-allocated)
        - id: UUID for cross-database sync
        - identifier: Business key (provider:timestamp:hash)

        Args:
            event_type: Type of event (push, ping, pull_request, etc.)
            payload: Full webhook payload JSON
            branch: Git branch name
            commit_sha: Commit hash
            sender: Who sent the event
            git_provider: Git provider (github, gitlab, gitea, bitbucket)

        Returns:
            pk_webhook_event (INTEGER primary key)
        """
        import hashlib
        import uuid

        now = datetime.now().isoformat()
        webhook_uuid = str(uuid.uuid4())
        # Create business key: provider:timestamp:hash(first 8 chars of payload hash)
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()[:8]
        identifier = f"{git_provider}:{now}:{payload_hash}"

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tb_webhook_event
                    (id, identifier, received_at, event_type,
                     git_provider, branch_name, commit_sha,
                     sender, payload, processed,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    webhook_uuid,
                    identifier,
                    now,
                    event_type,
                    git_provider,
                    branch,
                    commit_sha,
                    sender,
                    payload,
                    now,
                    now,
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def link_webhook_to_deployment(self, webhook_id: int, deployment_id: int) -> None:
        """Link a webhook event to its triggered deployment.

        Args:
            webhook_id: pk_webhook_event (INTEGER primary key)
            deployment_id: pk_deployment (INTEGER primary key) to link to
        """
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE tb_webhook_event
                SET processed=1, fk_deployment=?, updated_at=?
                WHERE pk_webhook_event=?
                """,
                (deployment_id, now, webhook_id),
            )
            conn.commit()

    def get_recent_webhooks(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get recent webhook events with trinity identifiers.

        Args:
            limit: Number of webhook events to return

        Returns:
            List of webhook events from v_webhook_event_history
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT pk_webhook_event, id, identifier, git_provider, event_type,
                       branch_name, commit_sha, sender, received_at, processed,
                       fk_deployment, deployment_id, fraise_name, environment_name,
                       created_at, updated_at
                FROM v_webhook_event_history
                ORDER BY received_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._normalize_webhook_dict(dict(row)) for row in rows]

"""Gitea provider implementation."""

import hashlib
import hmac
from typing import Any

from .base import GitProvider, WebhookEvent


class GiteaProvider(GitProvider):
    """Gitea Git provider.

    Gitea is a self-hosted Git service (fork of Gogs).
    Also compatible with Forgejo.
    """

    name = "gitea"
    signature_header = "X-Gitea-Signature"
    event_header = "X-Gitea-Event"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.webhook_secret = config.get("webhook_secret") or config.get("secret")

    def get_default_base_url(self) -> str:
        # No default - must be configured for self-hosted
        return self.config.get("base_url", "https://gitea.example.com")

    def _verify_signature(self, payload: bytes, headers: dict[str, str]) -> bool:
        """Verify Gitea webhook signature using HMAC-SHA256."""
        # Gitea supports multiple signature headers
        signature = (
            headers.get("X-Gitea-Signature")
            or headers.get("X-Gogs-Signature")
            or headers.get("X-Hub-Signature-256")  # GitHub-compatible mode
        )

        if not signature:
            return False

        # Remove prefix if present
        signature = signature.removeprefix("sha256=")

        expected = hmac.new(
            self.webhook_secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    def parse_webhook_event(
        self, headers: dict[str, str], payload: dict
    ) -> WebhookEvent:
        """Parse Gitea webhook payload.

        Gitea webhooks are similar to GitHub's format.
        """
        event_type = (
            headers.get("X-Gitea-Event")
            or headers.get("X-Gogs-Event")
            or headers.get("X-GitHub-Event")
            or "unknown"
        )

        branch = None
        commit_sha = None
        sender = None
        repository = None
        is_push = False
        is_tag = False
        is_merge_request = False
        is_ping = False

        # Extract repository
        repo_data = payload.get("repository", {})
        repository = repo_data.get("full_name")

        # Extract sender
        sender_data = payload.get("sender", {})
        pusher_data = payload.get("pusher", {})
        sender = (
            sender_data.get("login")
            or sender_data.get("username")
            or pusher_data.get("username")
        )

        if event_type == "push":
            is_push = True
            ref = payload.get("ref", "")

            if ref.startswith("refs/heads/"):
                branch = ref.replace("refs/heads/", "")
            elif ref.startswith("refs/tags/"):
                is_tag = True
                branch = ref.replace("refs/tags/", "")

            after = payload.get("after")
            commit_sha = after[:8] if after else None

        elif event_type == "create":
            ref_type = payload.get("ref_type")
            if ref_type == "tag":
                is_tag = True
                is_push = True
            branch = payload.get("ref")

        elif event_type == "pull_request":
            is_merge_request = True
            pr_data = payload.get("pull_request", {})
            head = pr_data.get("head", {})
            branch = head.get("ref")
            commit_sha = head.get("sha")

        return WebhookEvent(
            provider=self.name,
            event_type=event_type,
            branch=branch,
            commit_sha=commit_sha,
            sender=sender,
            repository=repository,
            raw_payload=payload,
            is_push=is_push,
            is_tag=is_tag,
            is_merge_request=is_merge_request,
            is_ping=is_ping,
        )


# Alias for convenience
Gitea = GiteaProvider

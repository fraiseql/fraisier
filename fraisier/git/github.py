"""GitHub provider implementation.

Replay protection
-----------------
Webhook deliveries are deduplicated using the ``X-GitHub-Delivery`` UUID header.
After HMAC signature verification, every request is checked against a TTL'd
in-memory LRU set (``_delivery_dedupe``).  Deliveries seen within
``WEBHOOK_DEDUPE_WINDOW_SECONDS`` (10 min) are rejected with HTTP 409.

**Single-process limitation**: the in-memory store is not shared across
processes or across daemon restarts.  If fraisier ever runs multiple webhook
workers (e.g. behind a load balancer), this MUST be replaced with a shared
store (SQLite or similar).
"""

import hashlib
import hmac
import threading
import time
from typing import Any

from fraisier.constants import WEBHOOK_DEDUPE_MAX_ENTRIES, WEBHOOK_DEDUPE_WINDOW_SECONDS

from .base import GitProvider, WebhookEvent


class _DeliveryDedupe:
    """TTL'd LRU set of recently-seen X-GitHub-Delivery UUIDs.

    Single-process only.  If fraisier ever runs multiple webhook workers,
    this MUST be replaced with a shared store (SQLite or similar).
    """

    def __init__(self, max_entries: int, ttl_seconds: int) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._store: dict[str, float] = {}  # {delivery_id: first_seen_at}
        self._lock = threading.Lock()

    def seen(self, delivery_id: str) -> bool:
        """Return True if *delivery_id* was already seen (replay), else record it."""
        with self._lock:
            now = time.time()
            self._prune(now)
            if delivery_id in self._store:
                return True
            if len(self._store) >= self._max:
                # Evict the entry with the oldest timestamp
                oldest = min(self._store, key=self._store.__getitem__)
                del self._store[oldest]
            self._store[delivery_id] = now
            return False

    def _prune(self, now: float) -> None:
        # Must be called with self._lock held.
        expired = [k for k, t in self._store.items() if now - t > self._ttl]
        for k in expired:
            del self._store[k]


_delivery_dedupe = _DeliveryDedupe(
    max_entries=WEBHOOK_DEDUPE_MAX_ENTRIES,
    ttl_seconds=WEBHOOK_DEDUPE_WINDOW_SECONDS,
)


class GitHubProvider(GitProvider):
    """GitHub Git provider.

    Supports:
    - github.com (default)
    - GitHub Enterprise (self-hosted)
    """

    name = "github"
    signature_header = "x-hub-signature-256"
    event_header = "x-github-event"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        # Support both naming conventions
        self.webhook_secret = config.get("webhook_secret") or config.get("secret")

    def get_default_base_url(self) -> str:
        return "https://github.com"

    def _verify_signature(self, payload: bytes, headers: dict[str, str]) -> bool:
        """Verify GitHub webhook signature using HMAC-SHA256."""
        signature = headers.get(self.get_signature_header_name())
        if not signature:
            return False

        assert self.webhook_secret is not None
        expected = (
            "sha256="
            + hmac.new(
                self.webhook_secret.encode(),
                payload,
                hashlib.sha256,
            ).hexdigest()
        )

        return hmac.compare_digest(expected, signature)

    def parse_webhook_event(
        self, headers: dict[str, str], payload: dict
    ) -> WebhookEvent:
        """Parse GitHub webhook payload."""
        event_type = headers.get(self.get_event_header_name(), "unknown")

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
        sender = sender_data.get("login") or payload.get("pusher", {}).get("name")

        if event_type == "push":
            is_push = True
            ref = payload.get("ref", "")

            if ref.startswith("refs/heads/"):
                branch = ref.replace("refs/heads/", "")
            elif ref.startswith("refs/tags/"):
                is_tag = True
                branch = ref.replace("refs/tags/", "")

            commit_sha = payload.get("after") or payload.get("head_commit", {}).get(
                "id"
            )

        elif event_type == "pull_request":
            is_merge_request = True
            pr_data = payload.get("pull_request", {})
            head = pr_data.get("head", {})
            branch = head.get("ref")
            commit_sha = head.get("sha")

        elif event_type == "ping":
            is_ping = True

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
GitHub = GitHubProvider

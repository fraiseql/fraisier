"""Abstract Git provider interface.

Any Git platform (GitHub, GitLab, Gitea, Bitbucket, self-hosted) can be
supported by implementing this interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class WebhookEvent:
    """Normalized webhook event from any Git provider."""

    event_type: str = "unknown"  # "push", "merge_request", "pull_request", "tag", etc.
    provider: str = "unknown"  # "github", "gitlab", "gitea", "bitbucket", etc.
    branch: str | None = None  # Branch name (for push events)
    commit_sha: str | None = None  # Commit SHA
    sender: str | None = None  # Username who triggered the event
    repository: str | None = None  # Repository name (owner/repo)
    raw_payload: dict | None = None  # Original payload for provider-specific data

    # Normalized event types
    is_push: bool = False
    is_tag: bool = False
    is_merge_request: bool = False  # PR/MR
    is_ping: bool = False

    def __post_init__(self):
        """Auto-detect event type flags if not explicitly set."""
        if self.raw_payload is None:
            self.raw_payload = {}
        # Auto-detect from event_type when flags not explicitly set
        if not self.is_push and self.event_type == "push":
            self.is_push = True
        if not self.is_ping and self.event_type == "ping":
            self.is_ping = True
        if not self.is_merge_request and self.event_type in (
            "pull_request",
            "merge_request",
        ):
            self.is_merge_request = True

    @property
    def is_pull_request(self) -> bool:
        """Alias for is_merge_request."""
        return self.is_merge_request


class GitProvider(ABC):
    """Abstract base class for Git providers.

    Implement this interface to add support for any Git hosting platform.
    """

    name: str  # Provider identifier (e.g., "github", "gitlab")
    signature_header: str  # Header containing webhook signature
    event_header: str  # Header containing event type

    def __init__(self, config: dict[str, Any]):
        """Initialize provider with configuration.

        Args:
            config: Provider-specific configuration from fraises.yaml
        """
        self.config = config
        self.webhook_secret = config.get("webhook_secret")

    def verify_webhook_signature(self, payload: bytes, headers: dict[str, str]) -> bool:
        """Verify webhook signature.

        Returns False when no secret is configured (secure default).
        Subclasses implement _verify_signature for provider-specific HMAC logic.
        """
        if not self.webhook_secret:
            return False
        return self._verify_signature(payload, headers)

    @abstractmethod
    def _verify_signature(self, payload: bytes, headers: dict[str, str]) -> bool:
        """Provider-specific signature verification."""
        pass

    @abstractmethod
    def parse_webhook_event(
        self, headers: dict[str, str], payload: dict
    ) -> WebhookEvent:
        """Parse webhook payload into normalized event."""
        pass

    def get_signature_header_name(self) -> str:
        """Get the header name containing the webhook signature."""
        return self.signature_header

    def get_event_header_name(self) -> str:
        """Get the header name containing the event type."""
        return self.event_header

    def get_clone_url(self, repository: str) -> str:
        """Get clone URL for a repository.

        Args:
            repository: Repository identifier (e.g., "owner/repo")

        Returns:
            Git clone URL
        """
        base_url = self.config.get("base_url", self.get_default_base_url())
        return f"{base_url}/{repository}.git"

    @abstractmethod
    def get_default_base_url(self) -> str:
        """Get default base URL for this provider.

        Returns:
            Base URL (e.g., "https://github.com")
        """
        pass

"""Git provider abstraction for Fraisier.

Supports any Git platform: GitHub, GitLab, Gitea, Bitbucket, or self-hosted.
"""

from .base import GitProvider, WebhookEvent
from .operations import clone_bare_repo, fetch_and_checkout, get_worktree_sha
from .registry import get_provider, list_providers, register_provider

__all__ = [
    "GitProvider",
    "WebhookEvent",
    "clone_bare_repo",
    "fetch_and_checkout",
    "get_provider",
    "get_worktree_sha",
    "list_providers",
    "register_provider",
]

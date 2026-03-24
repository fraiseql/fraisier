"""NotificationDispatcher: fan-out deploy events to configured notifiers."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fraisier.notifications.base import DeployEvent, Notifier

logger = logging.getLogger(__name__)


def _expand_env(value: str) -> str:
    """Expand ${VAR} references in a string."""
    if "${" not in value:
        return value
    return os.path.expandvars(value)


def _build_notifier(cfg: dict[str, Any]) -> Notifier:
    """Build a single Notifier from a config dict."""
    ntype = cfg["type"]

    if ntype in ("github_issue", "gitlab_issue", "gitea_issue", "bitbucket_issue"):
        from fraisier.notifications.git_issue import GitIssueNotifier
        from fraisier.notifications.git_issues import GitIssueClient

        provider = ntype.replace("_issue", "")
        token = _expand_env(cfg.get("token", ""))
        repo = cfg.get("repo", "")
        api_base = cfg.get("api_base")
        client = GitIssueClient(provider, token, repo, api_base=api_base)
        return GitIssueNotifier(
            client,
            labels=cfg.get("labels", []),
            assignees=cfg.get("assignees", []),
        )

    if ntype == "slack":
        from fraisier.notifications.messaging import SlackNotifier

        return SlackNotifier(_expand_env(cfg["webhook_url"]))

    if ntype == "discord":
        from fraisier.notifications.messaging import DiscordNotifier

        return DiscordNotifier(_expand_env(cfg["webhook_url"]))

    if ntype == "webhook":
        from fraisier.notifications.messaging import WebhookNotifier

        return WebhookNotifier(
            url=_expand_env(cfg["url"]),
            headers=cfg.get("headers"),
            method=cfg.get("method", "POST"),
        )

    msg = f"Unknown notifier type: {ntype!r}"
    raise ValueError(msg)


class NotificationDispatcher:
    """Dispatch deploy events to a set of notifiers.

    Events are mapped to notifiers by event type (on_failure, on_rollback,
    on_success). Notification failures are logged but never raised.
    """

    def __init__(
        self,
        on_failure: list[Notifier] | None = None,
        on_rollback: list[Notifier] | None = None,
        on_success: list[Notifier] | None = None,
    ):
        self._handlers: dict[str, list[Notifier]] = {
            "failure": on_failure or [],
            "rollback": on_rollback or [],
            "success": on_success or [],
        }

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> NotificationDispatcher:
        """Build dispatcher from the ``notifications:`` section of fraises.yaml."""
        handlers: dict[str, list[Notifier]] = {}
        for key in ("on_failure", "on_rollback", "on_success"):
            event_type = key.removeprefix("on_")
            notifier_cfgs = config.get(key, [])
            handlers[event_type] = [_build_notifier(c) for c in notifier_cfgs]
        return cls(
            on_failure=handlers.get("failure", []),
            on_rollback=handlers.get("rollback", []),
            on_success=handlers.get("success", []),
        )

    def notify(self, event: DeployEvent) -> None:
        """Dispatch event to all matching notifiers. Never raises."""
        notifiers = self._handlers.get(event.event_type, [])
        for notifier in notifiers:
            try:
                notifier.notify(event)
            except Exception:
                logger.warning(
                    "Notification failed for %s (%s)",
                    type(notifier).__name__,
                    event.event_type,
                    exc_info=True,
                )

    @property
    def is_configured(self) -> bool:
        """True if at least one notifier is configured."""
        return any(self._handlers.values())

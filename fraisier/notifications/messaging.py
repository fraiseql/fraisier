"""Slack, Discord, and generic webhook notifiers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from fraisier.notifications.rendering import render_slack_text

if TYPE_CHECKING:
    from fraisier.notifications.base import DeployEvent

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> None:
    """POST JSON to a URL, raising on failure."""
    resp = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()


class SlackNotifier:
    """Send notifications to Slack via incoming webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def notify(self, event: DeployEvent) -> None:
        text = render_slack_text(event)
        _post_json(self.webhook_url, {"text": text})


class DiscordNotifier:
    """Send notifications to Discord via webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def notify(self, event: DeployEvent) -> None:
        text = render_slack_text(event)
        color = {
            "failure": 0xFF0000,
            "rollback": 0xFFA500,
            "success": 0x00FF00,
        }.get(event.event_type, 0x808080)
        embed = {
            "title": f"{event.fraise_name}/{event.environment}",
            "description": text,
            "color": color,
        }
        _post_json(self.webhook_url, {"embeds": [embed]})


class WebhookNotifier:
    """POST DeployEvent as JSON to an arbitrary URL."""

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        method: str = "POST",
    ):
        self.url = url
        self.headers = headers or {}
        self.method = method.upper()

    def notify(self, event: DeployEvent) -> None:
        payload = event.to_dict()
        if self.method == "POST":
            _post_json(self.url, payload, self.headers)
        else:
            resp = httpx.request(
                self.method,
                self.url,
                json=payload,
                headers=self.headers,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()

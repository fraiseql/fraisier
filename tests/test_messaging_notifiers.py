"""Tests for Slack, Discord, and WebhookNotifier."""

from unittest.mock import patch

import httpx

from fraisier.notifications.base import DeployEvent
from fraisier.notifications.messaging import (
    DiscordNotifier,
    SlackNotifier,
    WebhookNotifier,
)


def _event(**kwargs) -> DeployEvent:
    defaults = {
        "fraise_name": "api",
        "environment": "prod",
        "event_type": "failure",
        "error_message": "boom",
        "duration_seconds": 5.0,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    defaults.update(kwargs)
    return DeployEvent(**defaults)


class TestSlackNotifier:
    def test_sends_text_to_webhook(self):
        with patch.object(httpx, "post") as mock_post:
            mock_post.return_value.raise_for_status = lambda: None
            notifier = SlackNotifier("https://hooks.slack.com/xxx")
            notifier.notify(_event())

            mock_post.assert_called_once()
            url = mock_post.call_args.args[0]
            assert url == "https://hooks.slack.com/xxx"
            payload = mock_post.call_args.kwargs["json"]
            assert "text" in payload
            assert "api/prod" in payload["text"]

    def test_success_event(self):
        with patch.object(httpx, "post") as mock_post:
            mock_post.return_value.raise_for_status = lambda: None
            notifier = SlackNotifier("https://hooks.slack.com/xxx")
            notifier.notify(_event(event_type="success", error_message=None))

            payload = mock_post.call_args.kwargs["json"]
            assert ":white_check_mark:" in payload["text"]


class TestDiscordNotifier:
    def test_sends_embed_to_webhook(self):
        with patch.object(httpx, "post") as mock_post:
            mock_post.return_value.raise_for_status = lambda: None
            notifier = DiscordNotifier("https://discord.com/api/webhooks/xxx")
            notifier.notify(_event())

            payload = mock_post.call_args.kwargs["json"]
            assert "embeds" in payload
            assert len(payload["embeds"]) == 1
            embed = payload["embeds"][0]
            assert embed["color"] == 0xFF0000  # red for failure
            assert "api/prod" in embed["title"]

    def test_success_green_color(self):
        with patch.object(httpx, "post") as mock_post:
            mock_post.return_value.raise_for_status = lambda: None
            notifier = DiscordNotifier("https://discord.com/api/webhooks/xxx")
            notifier.notify(_event(event_type="success", error_message=None))

            embed = mock_post.call_args.kwargs["json"]["embeds"][0]
            assert embed["color"] == 0x00FF00

    def test_rollback_orange_color(self):
        with patch.object(httpx, "post") as mock_post:
            mock_post.return_value.raise_for_status = lambda: None
            notifier = DiscordNotifier("https://discord.com/api/webhooks/xxx")
            notifier.notify(_event(event_type="rollback"))

            embed = mock_post.call_args.kwargs["json"]["embeds"][0]
            assert embed["color"] == 0xFFA500


class TestWebhookNotifier:
    def test_posts_deploy_event_dict(self):
        with patch.object(httpx, "post") as mock_post:
            mock_post.return_value.raise_for_status = lambda: None
            notifier = WebhookNotifier(
                "https://example.com/hook",
                headers={"X-Custom": "val"},
            )
            notifier.notify(_event())

            mock_post.assert_called_once()
            url = mock_post.call_args.args[0]
            assert url == "https://example.com/hook"
            payload = mock_post.call_args.kwargs["json"]
            assert payload["fraise_name"] == "api"
            assert payload["event_type"] == "failure"
            headers = mock_post.call_args.kwargs["headers"]
            assert headers["X-Custom"] == "val"

    def test_custom_method(self):
        with patch.object(httpx, "request") as mock_req:
            mock_req.return_value.raise_for_status = lambda: None
            notifier = WebhookNotifier("https://example.com/hook", method="PUT")
            notifier.notify(_event())

            mock_req.assert_called_once()
            assert mock_req.call_args.args[0] == "PUT"

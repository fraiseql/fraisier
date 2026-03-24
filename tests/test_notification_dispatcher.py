"""Tests for NotificationDispatcher config parsing and dispatch."""

from unittest.mock import MagicMock

from fraisier.notifications.base import DeployEvent
from fraisier.notifications.dispatcher import NotificationDispatcher


def _event(event_type="failure") -> DeployEvent:
    return DeployEvent(
        fraise_name="api",
        environment="prod",
        event_type=event_type,
        error_message="boom" if event_type != "success" else None,
        duration_seconds=5.0,
        timestamp="2026-01-01T00:00:00+00:00",
    )


class TestDispatcherFromConfig:
    def test_empty_config(self):
        dispatcher = NotificationDispatcher.from_config({})
        assert not dispatcher.is_configured

    def test_slack_notifier_created(self):
        config = {
            "on_failure": [
                {"type": "slack", "webhook_url": "https://hooks.slack.com/xxx"}
            ]
        }
        dispatcher = NotificationDispatcher.from_config(config)
        assert dispatcher.is_configured
        assert len(dispatcher._handlers["failure"]) == 1

    def test_discord_notifier_created(self):
        config = {
            "on_rollback": [
                {"type": "discord", "webhook_url": "https://discord.com/xxx"}
            ]
        }
        dispatcher = NotificationDispatcher.from_config(config)
        assert len(dispatcher._handlers["rollback"]) == 1

    def test_webhook_notifier_created(self):
        config = {
            "on_success": [{"type": "webhook", "url": "https://example.com/hook"}]
        }
        dispatcher = NotificationDispatcher.from_config(config)
        assert len(dispatcher._handlers["success"]) == 1

    def test_github_issue_notifier_created(self):
        config = {
            "on_failure": [
                {
                    "type": "github_issue",
                    "token": "ghp_xxx",
                    "repo": "owner/repo",
                    "labels": ["deploy-failure"],
                }
            ]
        }
        dispatcher = NotificationDispatcher.from_config(config)
        assert len(dispatcher._handlers["failure"]) == 1

    def test_multiple_notifiers(self):
        config = {
            "on_failure": [
                {"type": "slack", "webhook_url": "https://hooks.slack.com/xxx"},
                {
                    "type": "github_issue",
                    "token": "ghp_xxx",
                    "repo": "owner/repo",
                },
            ]
        }
        dispatcher = NotificationDispatcher.from_config(config)
        assert len(dispatcher._handlers["failure"]) == 2


class TestDispatcherNotify:
    def test_dispatches_to_matching_handlers(self):
        notifier = MagicMock()
        dispatcher = NotificationDispatcher(on_failure=[notifier])
        event = _event("failure")
        dispatcher.notify(event)
        notifier.notify.assert_called_once_with(event)

    def test_does_not_dispatch_to_wrong_event_type(self):
        notifier = MagicMock()
        dispatcher = NotificationDispatcher(on_failure=[notifier])
        event = _event("success")
        dispatcher.notify(event)
        notifier.notify.assert_not_called()

    def test_notification_failure_swallowed(self):
        notifier = MagicMock()
        notifier.notify.side_effect = RuntimeError("network error")
        dispatcher = NotificationDispatcher(on_failure=[notifier])
        # Should not raise
        dispatcher.notify(_event("failure"))

    def test_all_notifiers_called_even_if_one_fails(self):
        bad = MagicMock()
        bad.notify.side_effect = RuntimeError("fail")
        good = MagicMock()
        dispatcher = NotificationDispatcher(on_failure=[bad, good])
        dispatcher.notify(_event("failure"))
        good.notify.assert_called_once()


class TestEnvVarExpansion:
    def test_expands_env_vars(self, monkeypatch):
        monkeypatch.setenv("SLACK_URL", "https://hooks.slack.com/real")
        config = {"on_failure": [{"type": "slack", "webhook_url": "${SLACK_URL}"}]}
        dispatcher = NotificationDispatcher.from_config(config)
        notifier = dispatcher._handlers["failure"][0]
        assert notifier.webhook_url == "https://hooks.slack.com/real"

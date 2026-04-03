"""Tests for Email and Teams notifiers."""

from unittest.mock import MagicMock, patch

from fraisier.notifications.base import DeployEvent
from fraisier.notifications.dispatcher import NotificationDispatcher
from fraisier.notifications.messaging import EmailNotifier, TeamsNotifier
from fraisier.notifications.rendering import render_email_html


def _event(event_type="failure") -> DeployEvent:
    return DeployEvent(
        fraise_name="api",
        environment="prod",
        event_type=event_type,
        error_message="Health check failed" if event_type != "success" else None,
        error_code="HEALTH_CHECK_FAILED" if event_type != "success" else None,
        recovery_hint="Check the health endpoint" if event_type != "success" else None,
        old_version="abc123",
        new_version="def456" if event_type == "success" else None,
        duration_seconds=12.5,
        timestamp="2026-04-03T10:00:00+00:00",
    )


class TestEmailRendering:
    def test_render_email_html_failure(self):
        html = render_email_html(_event("failure"))
        assert "Deployment Failure" in html
        assert "api" in html
        assert "prod" in html
        assert "Health check failed" in html
        assert "HEALTH_CHECK_FAILED" in html
        assert "Check the health endpoint" in html

    def test_render_email_html_success(self):
        html = render_email_html(_event("success"))
        assert "Deployment Success" in html
        assert "def456" in html
        assert "Health check failed" not in html

    def test_render_email_html_rollback(self):
        html = render_email_html(_event("rollback"))
        assert "Deployment Rollback" in html

    def test_render_email_html_has_timestamp(self):
        html = render_email_html(_event("failure"))
        assert "2026-04-03" in html


class TestEmailNotifier:
    @patch("fraisier.notifications.messaging.smtplib.SMTP")
    def test_sends_email_with_tls(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=587,
            from_email="deploy@example.com",
            to_emails=["team@example.com"],
            smtp_user="user",
            smtp_password="pass",
        )
        notifier.notify(_event("failure"))

        mock_smtp_class.assert_called_once_with("smtp.example.com", 587)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("user", "pass")
        mock_server.sendmail.assert_called_once()
        call_args = mock_server.sendmail.call_args
        assert call_args[0][0] == "deploy@example.com"
        assert call_args[0][1] == ["team@example.com"]

    @patch("fraisier.notifications.messaging.smtplib.SMTP")
    def test_sends_email_without_tls(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        notifier = EmailNotifier(
            smtp_host="localhost",
            smtp_port=25,
            from_email="deploy@example.com",
            to_emails=["team@example.com"],
            use_tls=False,
        )
        notifier.notify(_event("success"))

        mock_server.starttls.assert_not_called()
        mock_server.login.assert_not_called()
        mock_server.sendmail.assert_called_once()

    @patch("fraisier.notifications.messaging.smtplib.SMTP")
    def test_email_subject_contains_event_info(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        notifier = EmailNotifier(
            smtp_host="localhost",
            smtp_port=587,
            from_email="deploy@example.com",
            to_emails=["team@example.com"],
            subject_prefix="[Deploy]",
        )
        notifier.notify(_event("failure"))

        raw_msg = mock_server.sendmail.call_args[0][2]
        assert "[Deploy] FAILURE: api/prod" in raw_msg

    @patch("fraisier.notifications.messaging.smtplib.SMTP")
    def test_email_has_html_and_plain_parts(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        notifier = EmailNotifier(
            smtp_host="localhost",
            smtp_port=587,
            from_email="deploy@example.com",
            to_emails=["team@example.com"],
        )
        notifier.notify(_event("failure"))

        raw_msg = mock_server.sendmail.call_args[0][2]
        assert "text/plain" in raw_msg
        assert "text/html" in raw_msg

    @patch("fraisier.notifications.messaging.smtplib.SMTP")
    def test_email_multiple_recipients(self, mock_smtp_class):
        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        notifier = EmailNotifier(
            smtp_host="localhost",
            smtp_port=587,
            from_email="deploy@example.com",
            to_emails=["a@example.com", "b@example.com"],
        )
        notifier.notify(_event("failure"))

        call_args = mock_server.sendmail.call_args
        assert call_args[0][1] == ["a@example.com", "b@example.com"]


class TestTeamsNotifier:
    @patch("fraisier.notifications.messaging._post_json")
    def test_sends_adaptive_card(self, mock_post):
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        notifier.notify(_event("failure"))

        mock_post.assert_called_once()
        url, payload = mock_post.call_args[0]
        assert url == "https://teams.example.com/hook"
        assert payload["type"] == "message"
        attachments = payload["attachments"]
        assert len(attachments) == 1
        content_type = "application/vnd.microsoft.card.adaptive"
        assert attachments[0]["contentType"] == content_type

    @patch("fraisier.notifications.messaging._post_json")
    def test_card_body_contains_event_details(self, mock_post):
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        notifier.notify(_event("failure"))

        content = mock_post.call_args[0][1]["attachments"][0]["content"]
        body = content["body"]
        # Title block
        assert body[0]["text"] == "Deployment Failure"
        assert body[0]["color"] == "attention"
        # FactSet
        facts = body[1]["facts"]
        fact_titles = [f["title"] for f in facts]
        assert "Fraise" in fact_titles
        assert "Environment" in fact_titles

    @patch("fraisier.notifications.messaging._post_json")
    def test_card_success_color(self, mock_post):
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        notifier.notify(_event("success"))

        content = mock_post.call_args[0][1]["attachments"][0]["content"]
        assert content["body"][0]["color"] == "good"

    @patch("fraisier.notifications.messaging._post_json")
    def test_card_rollback_color(self, mock_post):
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        notifier.notify(_event("rollback"))

        content = mock_post.call_args[0][1]["attachments"][0]["content"]
        assert content["body"][0]["color"] == "warning"

    @patch("fraisier.notifications.messaging._post_json")
    def test_card_includes_error_message(self, mock_post):
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        notifier.notify(_event("failure"))

        content = mock_post.call_args[0][1]["attachments"][0]["content"]
        body = content["body"]
        error_blocks = [
            b for b in body
            if b.get("color") == "attention" and b["type"] == "TextBlock"
        ]
        assert any("Health check failed" in b["text"] for b in error_blocks)

    @patch("fraisier.notifications.messaging._post_json")
    def test_card_mention_on_failure(self, mock_post):
        notifier = TeamsNotifier(
            webhook_url="https://teams.example.com/hook",
            mention_on_failure="@oncall",
        )
        notifier.notify(_event("failure"))

        content = mock_post.call_args[0][1]["attachments"][0]["content"]
        body = content["body"]
        mention_blocks = [b for b in body if "cc @oncall" in b.get("text", "")]
        assert len(mention_blocks) == 1

    @patch("fraisier.notifications.messaging._post_json")
    def test_card_no_mention_on_success(self, mock_post):
        notifier = TeamsNotifier(
            webhook_url="https://teams.example.com/hook",
            mention_on_failure="@oncall",
        )
        notifier.notify(_event("success"))

        content = mock_post.call_args[0][1]["attachments"][0]["content"]
        body = content["body"]
        mention_blocks = [b for b in body if "cc @oncall" in b.get("text", "")]
        assert len(mention_blocks) == 0

    @patch("fraisier.notifications.messaging._post_json")
    def test_card_includes_version_on_success(self, mock_post):
        notifier = TeamsNotifier(webhook_url="https://teams.example.com/hook")
        notifier.notify(_event("success"))

        content = mock_post.call_args[0][1]["attachments"][0]["content"]
        facts = content["body"][1]["facts"]
        version_facts = [f for f in facts if f["title"] == "Version"]
        assert len(version_facts) == 1
        assert version_facts[0]["value"] == "def456"


class TestDispatcherEmailTeams:
    def test_teams_notifier_created(self):
        config = {
            "on_failure": [
                {"type": "teams", "webhook_url": "https://teams.example.com/hook"}
            ]
        }
        dispatcher = NotificationDispatcher.from_config(config)
        assert dispatcher.is_configured
        assert len(dispatcher._handlers["failure"]) == 1
        assert isinstance(dispatcher._handlers["failure"][0], TeamsNotifier)

    def test_email_notifier_created(self):
        config = {
            "on_failure": [
                {
                    "type": "email",
                    "from_email": "deploy@example.com",
                    "to_emails": ["team@example.com"],
                }
            ]
        }
        dispatcher = NotificationDispatcher.from_config(config)
        assert dispatcher.is_configured
        assert len(dispatcher._handlers["failure"]) == 1
        assert isinstance(dispatcher._handlers["failure"][0], EmailNotifier)

    def test_email_env_var_expansion(self, monkeypatch):
        monkeypatch.setenv("SMTP_PASS", "secret123")
        config = {
            "on_failure": [
                {
                    "type": "email",
                    "smtp_host": "smtp.example.com",
                    "smtp_password": "${SMTP_PASS}",
                    "from_email": "deploy@example.com",
                    "to_emails": ["team@example.com"],
                }
            ]
        }
        dispatcher = NotificationDispatcher.from_config(config)
        notifier = dispatcher._handlers["failure"][0]
        assert notifier.smtp_password == "secret123"

    def test_teams_with_mention(self):
        config = {
            "on_failure": [
                {
                    "type": "teams",
                    "webhook_url": "https://teams.example.com/hook",
                    "mention_on_failure": "@engineering",
                }
            ]
        }
        dispatcher = NotificationDispatcher.from_config(config)
        notifier = dispatcher._handlers["failure"][0]
        assert notifier.mention_on_failure == "@engineering"


class TestConfigValidation:
    def test_valid_teams_config(self, tmp_path):
        from fraisier.config import FraisierConfig

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
notifications:
  on_failure:
    - type: teams
      webhook_url: https://teams.example.com/hook
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        config = FraisierConfig(str(cfg))
        assert config.notifications is not None

    def test_teams_missing_webhook_url(self, tmp_path):
        import pytest

        from fraisier.config import FraisierConfig
        from fraisier.errors import ValidationError

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
notifications:
  on_failure:
    - type: teams
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        with pytest.raises(ValidationError, match="webhook_url"):
            FraisierConfig(str(cfg))

    def test_valid_email_config(self, tmp_path):
        from fraisier.config import FraisierConfig

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
notifications:
  on_failure:
    - type: email
      from_email: deploy@example.com
      to_emails:
        - team@example.com
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        config = FraisierConfig(str(cfg))
        assert config.notifications is not None

    def test_email_missing_from_email(self, tmp_path):
        import pytest

        from fraisier.config import FraisierConfig
        from fraisier.errors import ValidationError

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
notifications:
  on_failure:
    - type: email
      to_emails:
        - team@example.com
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        with pytest.raises(ValidationError, match="from_email"):
            FraisierConfig(str(cfg))

    def test_email_missing_to_emails(self, tmp_path):
        import pytest

        from fraisier.config import FraisierConfig
        from fraisier.errors import ValidationError

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
notifications:
  on_failure:
    - type: email
      from_email: deploy@example.com
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        with pytest.raises(ValidationError, match="to_emails"):
            FraisierConfig(str(cfg))

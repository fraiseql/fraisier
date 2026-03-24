"""Tests for the notification system."""

from fraisier.deployers.base import DeploymentResult, DeploymentStatus
from fraisier.notifications.base import DeployEvent, Notifier
from fraisier.notifications.rendering import render_issue_body, render_slack_text


class TestDeployEvent:
    """Tests for DeployEvent dataclass and factory."""

    def test_from_result_failure(self):
        result = DeploymentResult(
            success=False,
            status=DeploymentStatus.FAILED,
            old_version="abc123",
            duration_seconds=12.5,
            error_message="Health check failed",
        )
        event = DeployEvent.from_result(
            result=result,
            fraise_name="my_api",
            environment="production",
            triggered_by="webhook",
        )
        assert event.event_type == "failure"
        assert event.fraise_name == "my_api"
        assert event.environment == "production"
        assert event.error_message == "Health check failed"
        assert event.old_version == "abc123"
        assert event.duration_seconds == 12.5
        assert event.triggered_by == "webhook"

    def test_from_result_success(self):
        result = DeploymentResult(
            success=True,
            status=DeploymentStatus.SUCCESS,
            old_version="abc123",
            new_version="def456",
            duration_seconds=5.0,
        )
        event = DeployEvent.from_result(
            result=result,
            fraise_name="my_api",
            environment="production",
        )
        assert event.event_type == "success"
        assert event.new_version == "def456"

    def test_from_result_rollback(self):
        result = DeploymentResult(
            success=False,
            status=DeploymentStatus.ROLLED_BACK,
            old_version="abc123",
            new_version="prev789",
            duration_seconds=20.0,
            error_message="Health check failed; rolled back",
        )
        event = DeployEvent.from_result(
            result=result,
            fraise_name="my_api",
            environment="staging",
        )
        assert event.event_type == "rollback"

    def test_to_dict_serializes_all_fields(self):
        result = DeploymentResult(
            success=False,
            status=DeploymentStatus.FAILED,
            error_message="boom",
        )
        event = DeployEvent.from_result(
            result=result, fraise_name="api", environment="prod"
        )
        d = event.to_dict()
        assert d["fraise_name"] == "api"
        assert d["environment"] == "prod"
        assert d["event_type"] == "failure"
        assert d["error_message"] == "boom"
        assert "timestamp" in d

    def test_dedup_key(self):
        result = DeploymentResult(
            success=False,
            status=DeploymentStatus.FAILED,
            error_message="boom",
        )
        event = DeployEvent.from_result(
            result=result, fraise_name="api", environment="prod"
        )
        key = event.dedup_key
        assert "api" in key
        assert "prod" in key


class TestRendering:
    """Tests for notification body rendering."""

    def _make_event(self, **kwargs) -> DeployEvent:
        defaults = {
            "fraise_name": "my_api",
            "environment": "production",
            "event_type": "failure",
            "error_message": "Health check failed",
            "error_code": "HEALTH_CHECK_FAILED",
            "recovery_hint": "Check the health endpoint",
            "old_version": "abc123",
            "duration_seconds": 12.5,
            "timestamp": "2026-03-24T10:00:00+00:00",
        }
        defaults.update(kwargs)
        return DeployEvent(**defaults)

    def test_render_issue_body_failure(self):
        event = self._make_event()
        body = render_issue_body(event)
        assert "## Deployment Failure" in body
        assert "`my_api`" in body
        assert "`production`" in body
        assert "Health check failed" in body
        assert "HEALTH_CHECK_FAILED" in body
        assert "Check the health endpoint" in body
        assert "fraisier" in body

    def test_render_issue_body_success(self):
        event = self._make_event(
            event_type="success",
            error_message=None,
            error_code=None,
            recovery_hint=None,
            new_version="def456",
        )
        body = render_issue_body(event)
        assert "## Deployment Success" in body
        assert "def456" in body
        assert "Error" not in body

    def test_render_issue_body_rollback(self):
        event = self._make_event(
            event_type="rollback",
            new_version="prev789",
            error_message="Timed out; rolled back",
        )
        body = render_issue_body(event)
        assert "## Deployment Rollback" in body

    def test_render_slack_text_failure(self):
        event = self._make_event()
        text = render_slack_text(event)
        assert ":x:" in text
        assert "my_api/production" in text
        assert "Health check failed" in text

    def test_render_slack_text_success(self):
        event = self._make_event(event_type="success", error_message=None)
        text = render_slack_text(event)
        assert ":white_check_mark:" in text

    def test_render_slack_text_rollback(self):
        event = self._make_event(event_type="rollback")
        text = render_slack_text(event)
        assert ":warning:" in text


class TestNotifierProtocol:
    """Verify the Notifier protocol can be implemented."""

    def test_notifier_protocol_compliance(self):
        """A class with notify(DeployEvent) satisfies the Notifier protocol."""

        class MyNotifier:
            def notify(self, event: DeployEvent) -> None:
                pass

        notifier: Notifier = MyNotifier()
        assert hasattr(notifier, "notify")

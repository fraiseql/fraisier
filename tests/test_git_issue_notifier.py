"""Tests for GitIssueNotifier dedup and auto-close logic."""

from unittest.mock import MagicMock

from fraisier.notifications.base import DeployEvent
from fraisier.notifications.git_issue import GitIssueNotifier
from fraisier.notifications.git_issues import GITHUB_ISSUE_CONFIG


def _make_client():
    client = MagicMock()
    client.cfg = GITHUB_ISSUE_CONFIG
    return client


def _failure_event(**kwargs) -> DeployEvent:
    defaults = {
        "fraise_name": "api",
        "environment": "prod",
        "event_type": "failure",
        "error_message": "boom",
        "error_code": "HEALTH_CHECK_FAILED",
        "duration_seconds": 5.0,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    defaults.update(kwargs)
    return DeployEvent(**defaults)


def _success_event(**kwargs) -> DeployEvent:
    defaults = {
        "fraise_name": "api",
        "environment": "prod",
        "event_type": "success",
        "new_version": "abc123",
        "duration_seconds": 3.0,
        "timestamp": "2026-01-01T00:00:00+00:00",
    }
    defaults.update(kwargs)
    return DeployEvent(**defaults)


class TestGitIssueNotifierDedup:
    def test_first_failure_creates_issue(self):
        client = _make_client()
        client.find_open_issue.return_value = None
        notifier = GitIssueNotifier(client, labels=["deploy-failure"])

        notifier.notify(_failure_event())

        client.open_issue.assert_called_once()
        call_kwargs = client.open_issue.call_args.kwargs
        assert "deploy-failure" in call_kwargs["labels"]
        assert "api" in call_kwargs["title"]
        assert "prod" in call_kwargs["title"]

    def test_second_failure_comments_existing(self):
        client = _make_client()
        client.find_open_issue.return_value = {"number": 7, "title": "existing"}
        notifier = GitIssueNotifier(client)

        notifier.notify(_failure_event())

        client.comment_issue.assert_called_once()
        assert client.comment_issue.call_args.args[0] == 7
        client.open_issue.assert_not_called()

    def test_success_closes_existing_issue(self):
        client = _make_client()
        client.find_open_issue.return_value = {"number": 7, "title": "existing"}
        notifier = GitIssueNotifier(client)

        notifier.notify(_success_event())

        client.comment_issue.assert_called_once()
        client.close_issue.assert_called_once_with(7)

    def test_success_no_issue_does_nothing(self):
        client = _make_client()
        client.find_open_issue.return_value = None
        notifier = GitIssueNotifier(client)

        notifier.notify(_success_event())

        client.open_issue.assert_not_called()
        client.close_issue.assert_not_called()

    def test_rollback_creates_issue(self):
        client = _make_client()
        client.find_open_issue.return_value = None
        notifier = GitIssueNotifier(client, labels=["rolled-back"])

        notifier.notify(_failure_event(event_type="rollback"))

        client.open_issue.assert_called_once()
        assert "rolled-back" in client.open_issue.call_args.kwargs["labels"]

    def test_assignees_passed_through(self):
        client = _make_client()
        client.find_open_issue.return_value = None
        notifier = GitIssueNotifier(client, labels=["bug"], assignees=["oncall"])

        notifier.notify(_failure_event())

        call_kwargs = client.open_issue.call_args.kwargs
        assert call_kwargs["assignees"] == ["oncall"]

"""Git issue notifier with deduplication and auto-close."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fraisier.notifications.rendering import render_issue_body

if TYPE_CHECKING:
    from fraisier.notifications.base import DeployEvent
    from fraisier.notifications.git_issues import GitIssueClient

logger = logging.getLogger(__name__)


class GitIssueNotifier:
    """Creates/updates/closes git issues based on deployment events.

    Dedup logic:
    - On failure/rollback: search for open issue with dedup_key in title.
      If found, add comment. If not, create new issue.
    - On success: search for open issue and close it.
    """

    def __init__(
        self,
        client: GitIssueClient,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ):
        self.client = client
        self.labels = labels or []
        self.assignees = assignees or []

    def notify(self, event: DeployEvent) -> None:
        """Send notification by creating/updating/closing git issues."""
        if event.event_type == "success":
            self._handle_success(event)
        else:
            self._handle_failure(event)

    def _handle_failure(self, event: DeployEvent) -> None:
        """On failure/rollback: create or comment on issue."""
        body = render_issue_body(event)
        existing = self.client.find_open_issue(event.dedup_key)

        if existing:
            issue_id = existing[self.client.cfg.issue_id_field]
            logger.info("Commenting on existing issue #%s", issue_id)
            self.client.comment_issue(issue_id, body)
        else:
            logger.info("Creating new issue: %s", event.dedup_key)
            self.client.open_issue(
                title=event.dedup_key,
                body=body,
                labels=self.labels,
                assignees=self.assignees,
            )

    def _handle_success(self, event: DeployEvent) -> None:
        """On success: close any open issue for this fraise/env."""
        existing = self.client.find_open_issue(event.dedup_key)
        if existing:
            issue_id = existing[self.client.cfg.issue_id_field]
            body = render_issue_body(event)
            self.client.comment_issue(issue_id, body)
            logger.info("Closing resolved issue #%s", issue_id)
            self.client.close_issue(issue_id)

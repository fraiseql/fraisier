"""Slack, Discord, Teams, Email, and generic webhook notifiers."""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Any

import httpx

from fraisier.notifications.rendering import render_email_html, render_slack_text

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


class TeamsNotifier:
    """Send notifications to Microsoft Teams via incoming webhook."""

    def __init__(
        self,
        webhook_url: str,
        mention_on_failure: str | None = None,
    ):
        self.webhook_url = webhook_url
        self.mention_on_failure = mention_on_failure

    def notify(self, event: DeployEvent) -> None:
        color = {
            "failure": "attention",
            "rollback": "warning",
            "rollback_failed": "attention",
            "success": "good",
        }.get(event.event_type, "default")

        facts = [
            {"title": "Fraise", "value": event.fraise_name},
            {"title": "Environment", "value": event.environment},
            {"title": "Event", "value": event.event_type},
        ]
        if event.duration_seconds:
            duration = f"{event.duration_seconds:.1f}s"
            facts.append({"title": "Duration", "value": duration})
        if event.new_version:
            facts.append({"title": "Version", "value": event.new_version})

        body: list[dict[str, Any]] = [
            {
                "type": "TextBlock",
                "size": "medium",
                "weight": "bolder",
                "text": f"Deployment {event.event_type.replace('_', ' ').title()}",
                "color": color,
            },
            {
                "type": "FactSet",
                "facts": facts,
            },
        ]

        if event.error_message:
            body.append({
                "type": "TextBlock",
                "text": event.error_message,
                "wrap": True,
                "color": "attention",
            })

        is_failure = event.event_type in ("failure", "rollback_failed")
        if self.mention_on_failure and is_failure:
            body.append({
                "type": "TextBlock",
                "text": f"cc {self.mention_on_failure}",
            })

        card = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": body,
                    },
                }
            ],
        }
        _post_json(self.webhook_url, card)


class EmailNotifier:
    """Send deployment notifications via SMTP email."""

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        from_email: str,
        to_emails: list[str],
        smtp_user: str = "",
        smtp_password: str = "",
        subject_prefix: str = "[Fraisier]",
        use_tls: bool = True,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.from_email = from_email
        self.to_emails = to_emails
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.subject_prefix = subject_prefix
        self.use_tls = use_tls

    def notify(self, event: DeployEvent) -> None:
        subject = (
            f"{self.subject_prefix} {event.event_type.upper()}: "
            f"{event.fraise_name}/{event.environment}"
        )

        html_body = render_email_html(event)
        plain_body = render_slack_text(event)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_email
        msg["To"] = ", ".join(self.to_emails)
        msg.attach(MIMEText(plain_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            if self.use_tls:
                server.starttls()
            if self.smtp_user:
                server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.from_email, self.to_emails, msg.as_string())

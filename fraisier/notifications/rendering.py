"""Render notification bodies from DeployEvent using Jinja2 templates."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader

if TYPE_CHECKING:
    from fraisier.notifications.base import DeployEvent

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}m {secs:.0f}s"


def render_issue_body(event: DeployEvent) -> str:
    """Render a Markdown issue body from a DeployEvent."""
    template = _env.get_template("issue_body.md.j2")
    context = event.to_dict()
    context["duration_human"] = _format_duration(event.duration_seconds)
    return template.render(**context)


def render_slack_text(event: DeployEvent) -> str:
    """Render a plain-text summary for Slack/Discord messages."""
    emoji = {"failure": ":x:", "rollback": ":warning:", "success": ":white_check_mark:"}
    icon = emoji.get(event.event_type, ":grey_question:")
    line = f"{icon} *{event.fraise_name}/{event.environment}* — {event.event_type}"
    if event.error_message:
        line += f"\n> {event.error_message}"
    if event.duration_seconds:
        line += f"\n_Duration: {_format_duration(event.duration_seconds)}_"
    return line

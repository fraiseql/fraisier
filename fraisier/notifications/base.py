"""Core notification types: DeployEvent dataclass and Notifier protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from fraisier.deployers.base import DeploymentResult


@dataclass(frozen=True)
class DeployEvent:
    """Immutable snapshot of a deployment event for notification dispatch."""

    fraise_name: str
    environment: str
    event_type: str  # "failure" | "rollback" | "success"
    error_message: str | None = None
    error_code: str | None = None
    recovery_hint: str | None = None
    old_version: str | None = None
    new_version: str | None = None
    duration_seconds: float = 0.0
    triggered_by: str = "deploy"
    commit_sha: str | None = None
    incident_path: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def from_result(
        cls,
        result: DeploymentResult,
        fraise_name: str,
        environment: str,
        triggered_by: str = "deploy",
        incident_path: str | None = None,
    ) -> DeployEvent:
        """Create a DeployEvent from a DeploymentResult."""
        if result.success:
            event_type = "success"
        elif result.status.value == "rolled_back":
            event_type = "rollback"
        else:
            event_type = "failure"

        error_code = None
        recovery_hint = None
        if result.error:
            error_code = getattr(result.error, "code", None)
            recovery_hint = getattr(result.error, "recovery_hint", None)

        return cls(
            fraise_name=fraise_name,
            environment=environment,
            event_type=event_type,
            error_message=result.error_message,
            error_code=error_code,
            recovery_hint=recovery_hint,
            old_version=result.old_version,
            new_version=result.new_version,
            duration_seconds=result.duration_seconds,
            triggered_by=triggered_by,
            commit_sha=result.new_version,
            incident_path=incident_path,
        )

    @property
    def dedup_key(self) -> str:
        """Key for issue deduplication: fraise/env/error_code."""
        code = self.error_code or "unknown"
        return f"[fraisier] {self.fraise_name}/{self.environment} {code}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON payloads."""
        return {
            "fraise_name": self.fraise_name,
            "environment": self.environment,
            "event_type": self.event_type,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "recovery_hint": self.recovery_hint,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "duration_seconds": self.duration_seconds,
            "triggered_by": self.triggered_by,
            "commit_sha": self.commit_sha,
            "incident_path": self.incident_path,
            "timestamp": self.timestamp,
        }


@runtime_checkable
class Notifier(Protocol):
    """Protocol for notification backends."""

    def notify(self, event: DeployEvent) -> None: ...

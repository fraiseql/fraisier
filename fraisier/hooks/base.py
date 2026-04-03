"""Core hook types: HookPhase, HookContext, Hook protocol, and HookRunner."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class HookPhase(Enum):
    """Deployment lifecycle phases where hooks can execute."""

    BEFORE_DEPLOY = "before_deploy"
    AFTER_DEPLOY = "after_deploy"
    BEFORE_ROLLBACK = "before_rollback"
    AFTER_ROLLBACK = "after_rollback"
    ON_FAILURE = "on_failure"


@dataclass(frozen=True)
class HookContext:
    """Immutable context passed to hooks during execution."""

    fraise_name: str
    environment: str
    phase: HookPhase
    config: dict[str, Any] = field(default_factory=dict)
    old_version: str | None = None
    new_version: str | None = None
    error_message: str | None = None


@dataclass
class HookResult:
    """Result of a single hook execution."""

    success: bool
    hook_name: str
    error: str | None = None


@runtime_checkable
class Hook(Protocol):
    """Protocol for lifecycle hooks."""

    @property
    def name(self) -> str: ...

    def execute(self, context: HookContext) -> HookResult: ...


class HookRunner:
    """Execute registered hooks for each deployment lifecycle phase.

    Hooks are registered per-phase and run in order. Hook failures
    in BEFORE_DEPLOY are fatal (abort deployment). All other phases
    are fire-and-forget (failures logged but not raised).
    """

    def __init__(self) -> None:
        self._hooks: dict[HookPhase, list[Hook]] = {phase: [] for phase in HookPhase}

    def register(self, phase: HookPhase, hook: Hook) -> None:
        """Register a hook for a lifecycle phase."""
        self._hooks[phase].append(hook)

    def run(self, phase: HookPhase, context: HookContext) -> list[HookResult]:
        """Run all hooks for a phase. Returns list of results.

        For BEFORE_DEPLOY, raises on first failure to abort deployment.
        For all other phases, failures are logged but swallowed.
        """
        results: list[HookResult] = []
        for hook in self._hooks[phase]:
            try:
                result = hook.execute(context)
                results.append(result)
                if not result.success and phase == HookPhase.BEFORE_DEPLOY:
                    raise HookAbortError(
                        f"Pre-deploy hook '{hook.name}' failed: {result.error}"
                    )
            except HookAbortError:
                raise
            except Exception as exc:
                result = HookResult(
                    success=False,
                    hook_name=hook.name,
                    error=str(exc),
                )
                results.append(result)
                if phase == HookPhase.BEFORE_DEPLOY:
                    raise HookAbortError(
                        f"Pre-deploy hook '{hook.name}' failed: {exc}"
                    ) from exc
                logger.warning(
                    "Hook '%s' failed in phase %s: %s",
                    hook.name,
                    phase.value,
                    exc,
                )
        return results

    @property
    def is_configured(self) -> bool:
        """True if at least one hook is registered."""
        return any(hooks for hooks in self._hooks.values())


class HookAbortError(Exception):
    """Raised when a pre-deploy hook fails and deployment must abort."""

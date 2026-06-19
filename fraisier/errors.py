"""Custom exception hierarchy for Fraisier.

Provides structured error types for different failure scenarios with:
- Error codes for programmatic handling
- Context preservation for debugging
- Recoverable flag for automated recovery
- Hierarchical structure for flexible exception handling
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fraisier.dbops.preflight import MigrationPreflightResult
    from fraisier.migration_analyzer import ErrorClassification


# Generic placeholder hint kept for backwards compatibility with
# ``format_for_cli`` callers. ``str(err)`` suppresses it so the trailing
# ``Recover with:`` line only appears for actionable, partial-state hints.
_GENERIC_HINT = "Check the logs for more details."


# Canonical recovery hints keyed by *scenario tag*, not class name. One
# error class may map to multiple scenarios; one scenario may be raised
# from multiple call sites. Keys must stay lowercase snake_case so they
# remain decoupled from the type hierarchy.
RECOVERY_HINTS: dict[str, str] = {
    "migration_preflight": (
        "rollback the restored snapshot, fix migrations, or run "
        "`confiture migrate baseline` to re-baseline."
    ),
    "migration_preflight_false_positive": (
        "if these migrations apply in order during a real `migrate up`, this is "
        "a preflight false positive caused by a skipped non-transactional "
        "migration — re-run `fraisier db restore` with `--skip-preflight`."
    ),
    "migrate_partial": (
        "`fraisier rollback <fraise> <env>` to restore the previous SHA "
        "and reverse the partial migration."
    ),
    "health_check_unhealthy": (
        "`fraisier rollback <fraise> <env>` — the new revision is live "
        "but failing its health check."
    ),
    "rollback_failed": (
        "manual intervention required: check `journalctl -u <service>`, "
        "inspect git HEAD, and verify database state before retrying."
    ),
    "deploy_timeout_unknown_phase": (
        "`fraisier diagnose <fraise> <env>` to determine which phase "
        "the deploy was in when the timeout fired."
    ),
}


class FrameworkError(Exception):
    """Base exception for all framework errors.

    All framework errors inherit from this and provide:
    - Standard error code for identification
    - Optional context dict for debugging
    - Recoverable flag for automated recovery
    - Recovery hint for actionable guidance
    """

    code: str = "FRAISIER_ERROR"
    recoverable: bool = False
    recovery_hint: str = _GENERIC_HINT

    def __init__(
        self,
        message: str,
        code: str | None = None,
        context: dict[str, Any] | None = None,
        recoverable: bool | None = None,
        cause: Exception | None = None,
        recovery_hint: str | None = None,
    ):
        """Initialize Fraisier error.

        Args:
            message: Human-readable error message
            code: Machine-readable error code (defaults to class code)
            context: Additional context dict for debugging
            recoverable: Whether error can be automatically recovered from
            cause: Original exception that caused this error
            recovery_hint: Per-instance recovery hint. When provided, overrides
                the class-level default and is rendered into ``str(err)`` as a
                trailing ``Recover with: <hint>`` line. Pass ``""`` (empty
                string) to explicitly suppress the trailing line entirely.
        """
        self.message = message
        self.code = code or self.__class__.code
        self.context = context or {}
        if recoverable is not None:
            self.recoverable = recoverable
        if recovery_hint is not None:
            self.recovery_hint = recovery_hint
        self.cause = cause

        # Include cause in message if present
        msg = message
        if cause:
            msg = f"{message} (caused by {type(cause).__name__}: {cause!s})"

        super().__init__(msg)

    def __str__(self) -> str:
        base = super().__str__()
        hint = self.recovery_hint
        if hint and hint != _GENERIC_HINT:
            return f"{base}\n\nRecover with: {hint}"
        return base

    def to_dict(self) -> dict[str, Any]:
        """Serialize error to dict for logging/API responses."""
        return {
            "error_type": self.__class__.__name__,
            "code": self.code,
            "message": self.message,
            "recovery_hint": self.recovery_hint,
            "context": self.context,
            "recoverable": self.recoverable,
        }

    def format_for_cli(self) -> str:
        """Format error for CLI display with consistent styling."""
        lines = [
            f"[red]Error:[/red] {self.message}",
            f"[dim]Code:[/dim] {self.code}",
        ]
        if self.recovery_hint:
            lines.append(f"[yellow]Suggestion:[/yellow] {self.recovery_hint}")
        if self.context:
            context_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
            lines.append(f"[dim]Context:[/dim] {context_str}")
        return "\n".join(lines)


class ConfigurationError(FrameworkError):
    """Configuration loading or validation errors."""

    code = "CONFIG_ERROR"


class DeploymentError(FrameworkError):
    """Deployment execution errors."""

    code = "DEPLOYMENT_ERROR"
    recovery_hint = (
        "Check deploy logs and retry. If the issue persists, "
        "inspect the service status on the target server."
    )


class DeploymentTimeoutError(DeploymentError):
    """Deployment operation timed out."""

    code = "DEPLOYMENT_TIMEOUT"
    recoverable = True
    recovery_hint = (
        "The operation timed out. Retry with a longer timeout "
        "or check if the target server is under heavy load."
    )


class HealthCheckError(DeploymentError):
    """Health check failed after deployment."""

    code = "HEALTH_CHECK_FAILED"
    recoverable = True
    recovery_hint = (
        "The service may still be starting. Check the health "
        "endpoint, service logs, and whether the port is listening."
    )


class ProviderError(FrameworkError):
    """Provider-related errors."""

    code = "PROVIDER_ERROR"
    recovery_hint = "Check the provider configuration in fraises.yaml."


class ProviderConnectionError(ProviderError):
    """Failed to connect to provider."""

    code = "PROVIDER_CONNECTION_ERROR"
    recoverable = True
    recovery_hint = (
        "SSH connection failed. Verify the host is reachable, "
        "SSH key is correct, and port is open."
    )


class ProviderUnavailableError(ProviderError):
    """Provider is temporarily unavailable."""

    code = "PROVIDER_UNAVAILABLE"
    recoverable = True
    recovery_hint = "The provider is temporarily unavailable. Wait and retry."


class ProviderConfigurationError(ProviderError):
    """Provider configuration is invalid."""

    code = "PROVIDER_CONFIG_ERROR"
    recovery_hint = (
        "Check the provider section in fraises.yaml for missing or invalid fields."
    )


class RollbackError(DeploymentError):
    """Rollback operation failed."""

    code = "ROLLBACK_FAILED"
    recovery_hint = (
        "Automatic rollback failed. Manual recovery may be "
        "required: check the database state and run "
        "confiture migrate down manually."
    )


class DatabaseError(FrameworkError):
    """Database operation errors."""

    code = "DATABASE_ERROR"
    recovery_hint = (
        "Check the database connection and migration files. "
        "Run 'confiture migrate status' to inspect migration state."
    )


class DatabaseConnectionError(DatabaseError):
    """Failed to connect to database."""

    code = "DATABASE_CONNECTION_ERROR"
    recoverable = True
    recovery_hint = (
        "Cannot connect to the database. Verify the connection "
        "string, that PostgreSQL is running, and that the "
        "database exists."
    )


class DatabaseTransactionError(DatabaseError):
    """Database transaction failed."""

    code = "DATABASE_TRANSACTION_ERROR"
    recoverable = True
    recovery_hint = (
        "A database transaction failed. Check for conflicting "
        "locks or schema errors, then retry."
    )


class MigrationError(DatabaseError):
    """Database migration operation failed.

    Includes detailed context about which migration failed, the database error,
    and rollback status to help operators debug and recover.
    """

    code = "MIGRATION_FAILED"
    recovery_hint = (
        "A database migration failed. Check the migration file and database "
        "state. See error details for recovery suggestions."
    )

    def __init__(
        self,
        message: str,
        code: str | None = None,
        context: dict[str, Any] | None = None,
        recoverable: bool | None = None,
        cause: Exception | None = None,
        recovery_hint: str | None = None,
        migration_file: str | None = None,
        direction: str | None = None,
        step: int | None = None,
        db_error: str | None = None,
        rollback_attempted: bool | None = None,
        rollback_succeeded: bool | None = None,
    ):
        """Initialize MigrationError with migration context.

        Args:
            message: Human-readable error message
            code: Machine-readable error code
            context: Additional context dict for debugging
            recoverable: Whether error can be automatically recovered from
            cause: Original exception that caused this error
            migration_file: Name of migration file that failed
                (e.g., "20260401_add_column.py")
            direction: Direction of migration ("up" or "down")
            step: Step number in migration sequence (1-indexed)
            db_error: Raw database error message from database engine
            rollback_attempted: Whether automatic rollback was attempted
            rollback_succeeded: Whether rollback succeeded (None if not attempted)
        """
        self.migration_file = migration_file
        self.direction = direction
        self.step = step
        self.db_error = db_error
        self.rollback_attempted = rollback_attempted
        self.rollback_succeeded = rollback_succeeded

        # Auto-classify error if db_error provided
        self.classification: ErrorClassification | None = None
        if db_error:
            from fraisier.migration_analyzer import classify_migration_error

            self.classification = classify_migration_error(db_error, direction or "up")

        # Build context dict with migration-specific fields
        migration_context: dict[str, object] = {
            "migration_file": migration_file,
            "direction": direction,
            "step": step,
            "db_error": db_error,
            "rollback_attempted": rollback_attempted,
            "rollback_succeeded": rollback_succeeded,
        }
        # Remove None values from context
        migration_context = {
            k: v for k, v in migration_context.items() if v is not None
        }

        # Add classification if available
        if self.classification:
            migration_context["classification"] = {
                "error_type": self.classification.error_type,
                "recoverable": self.classification.recoverable,
                "rollback_safe": self.classification.rollback_safe,
                "requires_manual_intervention": (
                    self.classification.requires_manual_intervention
                ),
            }

        # Merge with provided context
        merged_context = {**(context or {}), **migration_context}

        super().__init__(
            message=message,
            code=code,
            context=merged_context,
            recoverable=recoverable,
            cause=cause,
            recovery_hint=recovery_hint,
        )

    @property
    def migration_context_str(self) -> str:
        """Return formatted migration context for logging.

        Examples:
            "20260401_add_column.py (up, step 1)"
            "20260401_drop_column.py (down)"
            "(no migration context)"
        """
        if not self.migration_file:
            return "(no migration context)"

        parts = [self.migration_file]
        if self.direction:
            parts.append(self.direction)
        if self.step is not None:
            parts.append(f"step {self.step}")

        return f"{parts[0]} ({', '.join(parts[1:])})" if len(parts) > 1 else parts[0]

    @property
    def classification_str(self) -> str:
        """Return human-readable classification for logging.

        Examples:
            "constraint error"
            "transient error (recoverable)"
            "unknown error"
            "(no classification)"
        """
        if not self.classification:
            return "(no classification)"

        parts = [self.classification.error_type, "error"]
        if self.classification.recoverable:
            parts.append("(recoverable)")

        return " ".join(parts)

    def format_for_operator(self) -> str:
        """Return human-readable error message with recovery suggestions.

        Formats the error for operators who need to understand and fix the issue.
        Includes context, classification, recovery suggestions, and rollback status.

        Returns:
            Formatted error message suitable for display in CLI or logs
        """
        lines = ["Database migration failed!"]

        # Add migration context
        if self.migration_file:
            lines.append("")
            lines.append(f"Failed migration: {self.migration_file} ({self.direction})")
        if self.db_error:
            lines.append(f"Error: {self.db_error}")

        # Add rollback status
        if self.rollback_attempted is not None:
            lines.append("")
            if self.rollback_succeeded:
                lines.append("✓ Automatic rollback succeeded.")
            else:
                lines.append(
                    "✗ Automatic rollback FAILED — manual intervention required!"
                )

        # Add recovery suggestions
        if self.classification:
            from fraisier.migration_analyzer import get_recovery_suggestions

            suggestions = get_recovery_suggestions(
                self.classification.error_type,
                self.migration_file or "migration",
                self.db_error or "error",
            )
            lines.append("")
            lines.append("Recovery options:")
            for i, suggestion in enumerate(suggestions, 1):
                lines.append(f"{i}. {suggestion}")

        return "\n".join(lines)


class DeploymentLockError(DeploymentError):
    """Deployment lock acquisition failed."""

    code = "DEPLOYMENT_LOCKED"
    recoverable = True
    recovery_hint = (
        "Another deploy is in progress. Wait for it to finish, "
        "or remove a stale lock file if the previous deploy "
        "crashed."
    )


class NotFoundError(FrameworkError):
    """Requested resource not found."""

    code = "NOT_FOUND"


class ValidationError(FrameworkError):
    """Input validation failed."""

    code = "VALIDATION_ERROR"


class GitProviderError(FrameworkError):
    """Git provider related errors."""

    code = "GIT_PROVIDER_ERROR"


class MigrationPreflightError(DatabaseError):
    """Raised when migration preflight detects migrations that would fail.

    Carries a structured ``preflight_result`` so callers can inspect which
    migrations failed and display per-migration error messages.

    Attributes:
        preflight_result: The ``MigrationPreflightResult`` from the failed
            preflight run, or ``None`` if unavailable.
    """

    code = "MIGRATION_PREFLIGHT_FAILED"
    recoverable = True
    recovery_hint = (
        "Fix the failing migrations and redeploy. "
        "No database changes were made — the full restore was not started."
    )

    def __init__(
        self,
        message: str,
        preflight_result: MigrationPreflightResult | None = None,
        recovery_hint: str | None = None,
    ) -> None:
        super().__init__(message=message, recovery_hint=recovery_hint)
        self.preflight_result = preflight_result


class WebhookError(FrameworkError):
    """Webhook processing errors."""

    code = "WEBHOOK_ERROR"

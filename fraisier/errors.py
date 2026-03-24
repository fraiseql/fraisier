"""Custom exception hierarchy for Fraisier.

Provides structured error types for different failure scenarios with:
- Error codes for programmatic handling
- Context preservation for debugging
- Recoverable flag for automated recovery
- Hierarchical structure for flexible exception handling
"""

from typing import Any


class FraisierError(Exception):
    """Base exception for all Fraisier errors.

    All Fraisier errors inherit from this and provide:
    - Standard error code for identification
    - Optional context dict for debugging
    - Recoverable flag for automated recovery
    - Recovery hint for actionable guidance
    """

    code: str = "FRAISIER_ERROR"
    recoverable: bool = False
    recovery_hint: str = "Check the logs for more details."

    def __init__(
        self,
        message: str,
        code: str | None = None,
        context: dict[str, Any] | None = None,
        recoverable: bool | None = None,
        cause: Exception | None = None,
    ):
        """Initialize Fraisier error.

        Args:
            message: Human-readable error message
            code: Machine-readable error code (defaults to class code)
            context: Additional context dict for debugging
            recoverable: Whether error can be automatically recovered from
            cause: Original exception that caused this error
        """
        self.message = message
        self.code = code or self.__class__.code
        self.context = context or {}
        if recoverable is not None:
            self.recoverable = recoverable
        self.cause = cause

        # Include cause in message if present
        msg = message
        if cause:
            msg = f"{message} (caused by {type(cause).__name__}: {cause!s})"

        super().__init__(msg)

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


class ConfigurationError(FraisierError):
    """Configuration loading or validation errors."""

    code = "CONFIG_ERROR"


class DeploymentError(FraisierError):
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


class ProviderError(FraisierError):
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


class DatabaseError(FraisierError):
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


class DeploymentLockError(DeploymentError):
    """Deployment lock acquisition failed."""

    code = "DEPLOYMENT_LOCKED"
    recoverable = True
    recovery_hint = (
        "Another deploy is in progress. Wait for it to finish, "
        "or remove a stale lock file if the previous deploy "
        "crashed."
    )


class NotFoundError(FraisierError):
    """Requested resource not found."""

    code = "NOT_FOUND"


class ValidationError(FraisierError):
    """Input validation failed."""

    code = "VALIDATION_ERROR"


class GitProviderError(FraisierError):
    """Git provider related errors."""

    code = "GIT_PROVIDER_ERROR"


class WebhookError(FraisierError):
    """Webhook processing errors."""

    code = "WEBHOOK_ERROR"

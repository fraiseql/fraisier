"""Migration error analysis and classification for recovery guidance.

Analyzes database error messages from failed migrations to determine:
- Error type (constraint, syntax, transient, permission, unknown)
- Whether the error is recoverable (retry-able)
- Whether rollback is safe
- Whether manual intervention is required
"""

from dataclasses import dataclass

# Error pattern lists for classification
CONSTRAINT_PATTERNS = [
    "already exists",
    "duplicate",
    "unique constraint",
    "foreign key constraint",
    "not-null constraint",
    "check constraint",
    "exclusion constraint",
]

TRANSIENT_PATTERNS = [
    "timeout",
    "connection refused",
    "connection reset",
    "connection closed",
    "deadlock",
    "lock timeout",
    "try again",
    "temporarily unavailable",
]

SYNTAX_PATTERNS = [
    "syntax error",
    "unexpected token",
    "unexpected end of input",
    "parse error",
]

PERMISSION_PATTERNS = [
    "permission denied",
    "insufficient privilege",
    "access denied",
    "role",
]


@dataclass
class ErrorClassification:
    """Classification of a migration error with recovery guidance.

    Attributes:
        error_type: Category of error (constraint, syntax, transient, permission,
            unknown)
        recoverable: Whether error can be resolved by retrying
        rollback_safe: Whether it's safe to attempt rollback
        requires_manual_intervention: Whether human investigation is needed
    """

    error_type: str
    recoverable: bool
    rollback_safe: bool
    requires_manual_intervention: bool


def classify_migration_error(
    db_error: str, _direction: str = "up"
) -> ErrorClassification:
    """Classify a database error message to guide recovery.

    Analyzes error messages from database operations to determine the nature of
    the failure and appropriate recovery strategy.

    Examples:
        >>> c = classify_migration_error("column 'x' already exists", "up")
        >>> c.error_type
        'constraint'
        >>> c.requires_manual_intervention
        True

        >>> c = classify_migration_error("connection timeout", "up")
        >>> c.error_type
        'transient'
        >>> c.recoverable
        True

    Args:
        db_error: Raw error message from database
        _direction: Migration direction ("up" or "down") — reserved for
            future directional error handling

    Returns:
        ErrorClassification with error type and recovery guidance
    """
    if not db_error:
        return ErrorClassification(
            error_type="unknown",
            recoverable=False,
            rollback_safe=True,
            requires_manual_intervention=True,
        )

    error_lower = db_error.lower()

    # Constraint errors: typically non-recoverable, safe to rollback
    if any(pattern in error_lower for pattern in CONSTRAINT_PATTERNS):
        return ErrorClassification(
            error_type="constraint",
            recoverable=False,
            rollback_safe=True,
            requires_manual_intervention=True,
        )

    # Transient errors: recoverable via retry
    if any(pattern in error_lower for pattern in TRANSIENT_PATTERNS):
        return ErrorClassification(
            error_type="transient",
            recoverable=True,
            rollback_safe=True,
            requires_manual_intervention=False,
        )

    # Syntax errors: non-recoverable, require migration fix
    if any(pattern in error_lower for pattern in SYNTAX_PATTERNS):
        return ErrorClassification(
            error_type="syntax",
            recoverable=False,
            rollback_safe=True,
            requires_manual_intervention=True,
        )

    # Permission errors: non-recoverable, require privilege escalation
    if any(pattern in error_lower for pattern in PERMISSION_PATTERNS):
        return ErrorClassification(
            error_type="permission",
            recoverable=False,
            rollback_safe=True,
            requires_manual_intervention=True,
        )

    # Unknown error: assume worst case
    return ErrorClassification(
        error_type="unknown",
        recoverable=False,
        rollback_safe=True,
        requires_manual_intervention=True,
    )

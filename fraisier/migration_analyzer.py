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


def get_recovery_suggestions(
    error_type: str, migration_file: str, db_error: str
) -> list[str]:
    """Generate recovery suggestions for a migration error.

    Provides operator-friendly steps to resolve the issue based on error type.

    Args:
        error_type: Classification from classify_migration_error (e.g., "constraint")
        migration_file: Name of the migration that failed
        db_error: Database error message

    Returns:
        List of actionable recovery suggestions
    """
    suggestions_map = {
        "constraint": [
            "The migration constraint violation likely indicates the change "
            "was already applied to this database.",
            "Manually inspect the database state to verify current schema.",
            "If the migration is idempotent, retry the deployment.",
            "If not, manually fix the constraint and retry deployment.",
        ],
        "transient": [
            "This error is temporary (network or connection issue).",
            "Retry the deployment.",
            "If retries continue to fail, investigate database connectivity "
            "and network stability.",
        ],
        "syntax": [
            f"Migration {migration_file} contains a SQL syntax error.",
            f"Database error: {db_error}",
            "Fix the migration file and retry the deployment.",
        ],
        "permission": [
            "The database user lacks required permissions for this operation.",
            "Check that the migration user has appropriate privileges on "
            "the target database.",
            "Contact the database administrator to grant required permissions.",
        ],
        "unknown": [
            f"Database error: {db_error}",
            "Review the database logs for more details about this error.",
            "Consult documentation or contact support if the issue persists.",
        ],
    }

    return suggestions_map.get(error_type, suggestions_map["unknown"])

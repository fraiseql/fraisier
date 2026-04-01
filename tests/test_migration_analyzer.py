"""Tests for migration error analysis and classification."""


class TestErrorClassification:
    """Test error classification by database error message."""

    def test_classify_constraint_error_column_already_exists(self):
        """Test classifying 'column already exists' as constraint error."""
        from fraisier.migration_analyzer import classify_migration_error

        classification = classify_migration_error(
            "column 'new_column' already exists", "up"
        )

        assert classification.error_type == "constraint"
        assert classification.recoverable is False
        assert classification.rollback_safe is True
        assert classification.requires_manual_intervention is True

    def test_classify_constraint_error_duplicate_key(self):
        """Test classifying 'duplicate key value' as constraint error."""
        from fraisier.migration_analyzer import classify_migration_error

        classification = classify_migration_error(
            "duplicate key value violates unique constraint", "up"
        )

        assert classification.error_type == "constraint"

    def test_classify_transient_error_connection_timeout(self):
        """Test classifying connection timeout as transient error."""
        from fraisier.migration_analyzer import classify_migration_error

        classification = classify_migration_error("connection timeout", "up")

        assert classification.error_type == "transient"
        assert classification.recoverable is True
        assert classification.rollback_safe is True
        assert classification.requires_manual_intervention is False

    def test_classify_transient_error_connection_refused(self):
        """Test classifying connection refused as transient error."""
        from fraisier.migration_analyzer import classify_migration_error

        classification = classify_migration_error("connection refused", "up")

        assert classification.error_type == "transient"
        assert classification.recoverable is True

    def test_classify_transient_error_deadlock(self):
        """Test classifying deadlock as transient error."""
        from fraisier.migration_analyzer import classify_migration_error

        classification = classify_migration_error(
            "deadlock detected; see server log for query details", "up"
        )

        assert classification.error_type == "transient"
        assert classification.recoverable is True

    def test_classify_syntax_error(self):
        """Test classifying SQL syntax error."""
        from fraisier.migration_analyzer import classify_migration_error

        classification = classify_migration_error(
            "syntax error at or near 'CREAT'", "up"
        )

        assert classification.error_type == "syntax"
        assert classification.recoverable is False
        assert classification.requires_manual_intervention is True

    def test_classify_syntax_error_unexpected_token(self):
        """Test classifying unexpected token as syntax error."""
        from fraisier.migration_analyzer import classify_migration_error

        classification = classify_migration_error(
            "syntax error: unexpected token", "up"
        )

        assert classification.error_type == "syntax"

    def test_classify_permission_error_access_denied(self):
        """Test classifying access denied as permission error."""
        from fraisier.migration_analyzer import classify_migration_error

        classification = classify_migration_error(
            "permission denied for schema public", "up"
        )

        assert classification.error_type == "permission"
        assert classification.recoverable is False
        assert classification.requires_manual_intervention is True

    def test_classify_permission_error_insufficient_privilege(self):
        """Test classifying insufficient privilege as permission error."""
        from fraisier.migration_analyzer import classify_migration_error

        classification = classify_migration_error(
            "insufficient privilege for operation", "up"
        )

        assert classification.error_type == "permission"

    def test_classify_unknown_error(self):
        """Test classifying unknown error."""
        from fraisier.migration_analyzer import classify_migration_error

        classification = classify_migration_error("some random error message", "up")

        assert classification.error_type == "unknown"
        assert classification.recoverable is False
        assert classification.rollback_safe is True
        assert classification.requires_manual_intervention is True

    def test_error_classification_is_dataclass(self):
        """Test that classification result is a proper dataclass."""
        from fraisier.migration_analyzer import (
            ErrorClassification,
            classify_migration_error,
        )

        classification = classify_migration_error("test error", "up")

        assert isinstance(classification, ErrorClassification)
        assert hasattr(classification, "error_type")
        assert hasattr(classification, "recoverable")
        assert hasattr(classification, "rollback_safe")
        assert hasattr(classification, "requires_manual_intervention")

    def test_classify_case_insensitive(self):
        """Test that classification is case-insensitive."""
        from fraisier.migration_analyzer import classify_migration_error

        classification1 = classify_migration_error("CONNECTION TIMEOUT", "up")
        classification2 = classify_migration_error("connection timeout", "up")

        assert classification1.error_type == classification2.error_type
        assert classification1.error_type == "transient"

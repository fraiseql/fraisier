"""Tests for migration error recovery suggestions."""


class TestRecoverySuggestions:
    """Test recovery suggestion generation by error type."""

    def test_constraint_error_recovery_suggestions(self):
        """Test constraint error suggests database inspection and retry."""
        from fraisier.migration_analyzer import get_recovery_suggestions

        suggestions = get_recovery_suggestions(
            error_type="constraint",
            migration_file="20260401_add_column.py",
            db_error="column 'new_column' already exists",
        )

        assert len(suggestions) > 0
        assert any("inspect" in s.lower() for s in suggestions)
        assert any(
            "idempotent" in s.lower() or "already" in s.lower() for s in suggestions
        )

    def test_transient_error_recovery_suggestions(self):
        """Test transient error suggests retry."""
        from fraisier.migration_analyzer import get_recovery_suggestions

        suggestions = get_recovery_suggestions(
            error_type="transient",
            migration_file="20260401_add_column.py",
            db_error="connection timeout",
        )

        assert len(suggestions) > 0
        assert any("retry" in s.lower() for s in suggestions)

    def test_syntax_error_recovery_suggestions(self):
        """Test syntax error suggests fixing migration file."""
        from fraisier.migration_analyzer import get_recovery_suggestions

        suggestions = get_recovery_suggestions(
            error_type="syntax",
            migration_file="20260401_add_column.py",
            db_error="syntax error at or near 'CREAT'",
        )

        assert len(suggestions) > 0
        assert any("fix" in s.lower() or "migration" in s.lower() for s in suggestions)

    def test_permission_error_recovery_suggestions(self):
        """Test permission error suggests checking privileges."""
        from fraisier.migration_analyzer import get_recovery_suggestions

        suggestions = get_recovery_suggestions(
            error_type="permission",
            migration_file="20260401_add_column.py",
            db_error="permission denied for schema public",
        )

        assert len(suggestions) > 0
        assert any(
            "permission" in s.lower() or "privilege" in s.lower() for s in suggestions
        )

    def test_unknown_error_recovery_suggestions(self):
        """Test unknown error suggests checking logs and contacting support."""
        from fraisier.migration_analyzer import get_recovery_suggestions

        suggestions = get_recovery_suggestions(
            error_type="unknown",
            migration_file="20260401_add_column.py",
            db_error="some random error message",
        )

        assert len(suggestions) > 0
        assert any(
            "log" in s.lower() or "support" in s.lower() or "manual" in s.lower()
            for s in suggestions
        )

    def test_recovery_suggestions_are_list_of_strings(self):
        """Test that suggestions are returned as list of strings."""
        from fraisier.migration_analyzer import get_recovery_suggestions

        suggestions = get_recovery_suggestions(
            error_type="transient",
            migration_file="test.py",
            db_error="timeout",
        )

        assert isinstance(suggestions, list)
        assert all(isinstance(s, str) for s in suggestions)
        assert all(len(s) > 0 for s in suggestions)

    def test_recovery_suggestions_include_migration_file_context(self):
        """Test that suggestions reference the specific migration file."""
        from fraisier.migration_analyzer import get_recovery_suggestions

        suggestions = get_recovery_suggestions(
            error_type="syntax",
            migration_file="20260401_custom_migration.py",
            db_error="syntax error",
        )

        # At least one suggestion should mention the migration file
        assert any("20260401_custom_migration.py" in s for s in suggestions)

    def test_recovery_suggestions_for_all_error_types(self):
        """Test that all error types have recovery suggestions."""
        from fraisier.migration_analyzer import get_recovery_suggestions

        error_types = ["constraint", "transient", "syntax", "permission", "unknown"]

        for error_type in error_types:
            suggestions = get_recovery_suggestions(
                error_type=error_type,
                migration_file="test.py",
                db_error="error message",
            )
            assert len(suggestions) > 0, f"No suggestions for {error_type}"

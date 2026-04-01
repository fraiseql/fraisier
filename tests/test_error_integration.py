"""Integration tests for error formatting in results and display."""


class TestErrorIntegration:
    """Test error messages in deployment results and CLI output."""

    def test_migration_error_displays_recovery_suggestions(self):
        """Test that migration errors can be formatted for operator display."""
        from fraisier.errors import MigrationError

        error = MigrationError(
            "Migration failed",
            migration_file="20260401_add_column.py",
            direction="up",
            db_error="column 'new_column' already exists",
            rollback_attempted=True,
            rollback_succeeded=True,
        )

        formatted = error.format_for_operator()

        # Should display all essential information
        assert "Database migration failed" in formatted
        assert "20260401_add_column.py" in formatted
        assert "column 'new_column' already exists" in formatted
        assert "rollback" in formatted.lower()
        assert "succeeded" in formatted.lower()
        # Should include recovery section
        assert "recovery" in formatted.lower() or "option" in formatted.lower()

    def test_transient_error_displays_retry_suggestion(self):
        """Test transient error message suggests retry."""
        from fraisier.errors import MigrationError

        error = MigrationError(
            "Migration failed",
            migration_file="20260401_migrate.py",
            direction="up",
            db_error="connection timeout",
        )

        formatted = error.format_for_operator()

        # Transient error should mention retry
        assert "retry" in formatted.lower()

    def test_syntax_error_displays_fix_migration_suggestion(self):
        """Test syntax error message suggests fixing migration file."""
        from fraisier.errors import MigrationError

        error = MigrationError(
            "Migration failed",
            migration_file="20260401_bad_sql.py",
            direction="up",
            db_error="syntax error at or near 'CREAT'",
        )

        formatted = error.format_for_operator()

        # Should mention fixing the migration file
        assert "fix" in formatted.lower() or "migration" in formatted.lower()
        assert "20260401_bad_sql.py" in formatted

    def test_permission_error_displays_admin_contact_suggestion(self):
        """Test permission error message suggests contacting admin."""
        from fraisier.errors import MigrationError

        error = MigrationError(
            "Migration failed",
            migration_file="20260401_schema.py",
            direction="up",
            db_error="permission denied for schema public",
        )

        formatted = error.format_for_operator()

        # Should mention permission/privilege
        assert (
            "permission" in formatted.lower()
            or "privilege" in formatted.lower()
            or "admin" in formatted.lower()
        )

    def test_error_message_is_multiline_and_readable(self):
        """Test that formatted error message is properly structured."""
        from fraisier.errors import MigrationError

        error = MigrationError(
            "Migration failed",
            migration_file="20260401_test.py",
            direction="up",
            db_error="constraint violation",
            rollback_attempted=True,
            rollback_succeeded=True,
        )

        formatted = error.format_for_operator()
        lines = formatted.split("\n")

        # Should be multi-line for readability (with blank lines for spacing)
        assert len(lines) > 3
        # Should start with clear header
        assert lines[0].startswith("Database")
        # Should have content lines (may have blank lines for formatting)
        non_empty_lines = [line for line in lines if line.strip()]
        assert len(non_empty_lines) > 3

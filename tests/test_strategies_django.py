"""Tests for DjangoMigrateStrategy."""

from unittest.mock import patch

import pytest

pytest.importorskip("django", reason="django not installed")

from fraisier.strategies import DjangoMigrateStrategy


class TestDjangoMigrateStrategyValidateSetup:
    """Test Django migration setup validation."""

    def test_validate_setup_missing_manage_py(self, tmp_path):
        """validate_setup fails when manage.py is missing."""
        strategy = DjangoMigrateStrategy("settings")
        result = strategy.validate_setup(tmp_path)
        assert not result.valid
        assert any("manage.py" in e for e in result.errors)

    def test_validate_setup_valid(self, tmp_path):
        """validate_setup succeeds when manage.py exists and Django is installed."""
        (tmp_path / "manage.py").write_text("#!/usr/bin/env python\n")
        strategy = DjangoMigrateStrategy("settings")

        with patch("django.setup"):
            result = strategy.validate_setup(tmp_path)

        assert result.valid

    def test_validate_setup_django_not_installed(self, tmp_path):
        """validate_setup fails when Django setup raises exception."""
        (tmp_path / "manage.py").write_text("#!/usr/bin/env python\n")
        strategy = DjangoMigrateStrategy("settings")

        with patch("django.setup", side_effect=Exception("Django setup failed")):
            result = strategy.validate_setup(tmp_path)

        assert not result.valid
        assert any("Cannot setup Django" in e for e in result.errors)


class TestDjangoMigrateStrategyFrameworkName:
    """Test framework name property."""

    def test_framework_name(self):
        """framework_name returns 'django'."""
        strategy = DjangoMigrateStrategy("settings")
        assert strategy.framework_name == "django"


class TestDjangoMigrateStrategyMigrateUp:
    """Test Django migrate up."""

    def test_migrate_up_success(self, tmp_path):
        """migrate_up returns success when execute_from_command_line succeeds."""
        strategy = DjangoMigrateStrategy("settings")

        with (
            patch("os.chdir"),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch("fraisier.strategies.execute_from_command_line") as mock_execute,
        ):
            result = strategy.migrate_up(tmp_path)

        assert result.success
        mock_execute.assert_called_once()

    def test_migrate_up_failure(self, tmp_path):
        """migrate_up returns failure when execute_from_command_line raises."""
        strategy = DjangoMigrateStrategy("settings")

        with patch(
            "fraisier.strategies.execute_from_command_line",
            side_effect=Exception("migrate failed"),
        ):
            result = strategy.migrate_up(tmp_path)

        assert not result.success
        assert len(result.errors) > 0


class TestDjangoMigrateStrategyMigrateDown:
    """Test Django migrate down (rollback)."""

    def test_migrate_down_success(self, tmp_path):
        """migrate_down returns success when execute_from_command_line succeeds."""
        strategy = DjangoMigrateStrategy("settings")

        with (
            patch("os.chdir"),
            patch("pathlib.Path.cwd", return_value=tmp_path),
            patch("fraisier.strategies.execute_from_command_line") as mock_execute,
        ):
            result = strategy.migrate_down(tmp_path, "0001")

        assert result.success
        mock_execute.assert_called_once()


class TestDjangoMigrateStrategyGetCurrentVersion:
    """Test Django get current version."""

    def test_get_current_version_returns_none_on_exception(self, tmp_path):
        """get_current_version returns None when showmigrations fails."""
        strategy = DjangoMigrateStrategy("settings")

        with patch(
            "fraisier.strategies.execute_from_command_line",
            side_effect=Exception("showmigrations failed"),
        ):
            result = strategy.get_current_version(tmp_path)

        assert result is None

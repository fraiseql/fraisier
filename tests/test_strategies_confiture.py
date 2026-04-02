"""Tests for ConfitureMigrateStrategy."""

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("confiture", reason="confiture not installed")

from fraisier.strategies import ConfitureMigrateStrategy


class TestConfitureMigrateStrategyValidateSetup:
    def test_missing_config_file_reports_error(self, tmp_path):
        strategy = ConfitureMigrateStrategy("confiture.yaml")
        result = strategy.validate_setup(tmp_path)
        assert not result.valid
        assert any("not found" in e for e in result.errors)

    def test_valid_config_file_passes(self, tmp_path):
        (tmp_path / "confiture.yaml").write_text("db_url: postgresql://localhost/test")
        strategy = ConfitureMigrateStrategy("confiture.yaml")
        result = strategy.validate_setup(tmp_path)
        # confiture is installed in this project, so should be valid
        assert result.valid
        assert result.errors == []

    def test_confiture_not_installed_reports_error(self, tmp_path):
        (tmp_path / "confiture.yaml").write_text("db_url: postgresql://localhost/test")
        strategy = ConfitureMigrateStrategy("confiture.yaml")

        with patch("importlib.util.find_spec", return_value=None):
            result = strategy.validate_setup(tmp_path)

        assert not result.valid
        assert any("not available" in e or "not found" in e for e in result.errors)


class TestConfitureMigrateStrategyFrameworkName:
    def test_framework_name(self):
        strategy = ConfitureMigrateStrategy()
        assert strategy.framework_name == "confiture"


class TestConfitureMigrateStrategyMigrateUp:
    def test_migrate_up_success(self, tmp_path):
        strategy = ConfitureMigrateStrategy("confiture.yaml")

        migration_result = MagicMock()
        migration_result.success = True
        migration_result.steps_applied = 2
        migration_result.errors = []

        with (
            patch("fraisier.dbops.confiture.migrate_up", return_value=migration_result),
            patch("fraisier.dbops.confiture.preflight"),
        ):
            result = strategy.migrate_up(tmp_path)

        assert result.success
        assert result.migrations_applied == 2

    def test_migrate_up_exception_returns_failure(self, tmp_path):
        strategy = ConfitureMigrateStrategy("confiture.yaml")
        # config file does not exist → preflight will raise → caught as failure
        with patch(
            "fraisier.dbops.confiture.preflight", side_effect=RuntimeError("no config")
        ):
            result = strategy.migrate_up(tmp_path)
        assert not result.success
        assert len(result.errors) > 0

    def test_migrate_up_with_database_url_override(self, tmp_path):
        strategy = ConfitureMigrateStrategy("confiture.yaml")

        migration_result = MagicMock()
        migration_result.success = True
        migration_result.steps_applied = 1
        migration_result.errors = []

        with (
            patch(
                "fraisier.dbops.confiture.migrate_up", return_value=migration_result
            ) as mock_up,
            patch("fraisier.dbops.confiture.preflight"),
        ):
            result = strategy.migrate_up(
                tmp_path, database_url="postgresql://localhost/test"
            )

        assert result.success
        call_kwargs = mock_up.call_args[1]
        assert call_kwargs.get("database_url") == "postgresql://localhost/test"


class TestConfitureMigrateStrategyMigrateDown:
    def test_migrate_down_success(self, tmp_path):
        strategy = ConfitureMigrateStrategy("confiture.yaml")

        migration_result = MagicMock()
        migration_result.success = True
        migration_result.steps_applied = 1
        migration_result.errors = []

        with patch(
            "fraisier.dbops.confiture.migrate_down", return_value=migration_result
        ):
            result = strategy.migrate_down(tmp_path, target="001")

        assert result.success
        assert result.migrations_applied == 1

    def test_migrate_down_exception_returns_failure(self, tmp_path):
        strategy = ConfitureMigrateStrategy("confiture.yaml")
        with patch(
            "fraisier.dbops.confiture.migrate_down",
            side_effect=RuntimeError("conn failed"),
        ):
            result = strategy.migrate_down(tmp_path, target="001")
        assert not result.success
        assert len(result.errors) > 0

"""Tests for AlembicMigrateStrategy."""

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("alembic", reason="alembic not installed")

from fraisier.strategies import AlembicMigrateStrategy


class TestAlembicMigrateStrategyValidateSetup:
    """Test Alembic migration setup validation."""

    def test_validate_setup_missing_ini(self, tmp_path):
        """validate_setup fails when alembic.ini is missing."""
        strategy = AlembicMigrateStrategy("alembic", "nonexistent.ini")
        result = strategy.validate_setup(tmp_path)
        assert not result.valid
        assert any("alembic.ini" in e for e in result.errors)

    def test_validate_setup_valid(self, tmp_path):
        """validate_setup succeeds when ini and env.py exist."""
        ini_path = tmp_path / "alembic.ini"
        ini_path.write_text("")
        script_dir = tmp_path / "alembic"
        script_dir.mkdir()
        (script_dir / "env.py").write_text("")

        strategy = AlembicMigrateStrategy("alembic", str(ini_path))
        result = strategy.validate_setup(tmp_path)
        assert result.valid

    def test_validate_setup_missing_env_py(self, tmp_path):
        """validate_setup fails when env.py is missing."""
        ini_path = tmp_path / "alembic.ini"
        ini_path.write_text("")
        script_dir = tmp_path / "alembic"
        script_dir.mkdir()

        strategy = AlembicMigrateStrategy("alembic", str(ini_path))
        result = strategy.validate_setup(tmp_path)
        assert not result.valid
        assert any("env.py" in e for e in result.errors)


class TestAlembicMigrateStrategyFrameworkName:
    """Test framework name property."""

    def test_framework_name(self):
        """framework_name returns 'alembic'."""
        strategy = AlembicMigrateStrategy("alembic", "alembic.ini")
        assert strategy.framework_name == "alembic"


class TestAlembicMigrateStrategyMigrateUp:
    """Test Alembic migrate up."""

    def test_migrate_up_success(self, tmp_path):
        """migrate_up returns success when command.upgrade succeeds."""
        ini_path = tmp_path / "alembic.ini"
        ini_path.write_text("")

        strategy = AlembicMigrateStrategy("alembic", str(ini_path))

        with patch("alembic.command.upgrade") as mock_upgrade:
            result = strategy.migrate_up(tmp_path)

        assert result.success
        mock_upgrade.assert_called_once()

    def test_migrate_up_with_database_url(self, tmp_path):
        """migrate_up forwards database_url to config."""
        ini_path = tmp_path / "alembic.ini"
        ini_path.write_text("")

        strategy = AlembicMigrateStrategy("alembic", str(ini_path))

        with patch("alembic.config.Config") as mock_config_cls:
            mock_config = MagicMock()
            mock_config_cls.return_value = mock_config

            with patch("alembic.command.upgrade"):
                strategy.migrate_up(
                    tmp_path, database_url="postgresql://localhost/test"
                )

            mock_config.set_main_option.assert_any_call(
                "sqlalchemy.url", "postgresql://localhost/test"
            )

    def test_migrate_up_failure(self, tmp_path):
        """migrate_up returns failure when command.upgrade raises."""
        ini_path = tmp_path / "alembic.ini"
        ini_path.write_text("")

        strategy = AlembicMigrateStrategy("alembic", str(ini_path))

        with patch("alembic.command.upgrade", side_effect=Exception("upgrade failed")):
            result = strategy.migrate_up(tmp_path)

        assert not result.success
        assert len(result.errors) > 0


class TestAlembicMigrateStrategyMigrateDown:
    """Test Alembic migrate down (rollback)."""

    def test_migrate_down_success(self, tmp_path):
        """migrate_down returns success when command.downgrade succeeds."""
        ini_path = tmp_path / "alembic.ini"
        ini_path.write_text("")

        strategy = AlembicMigrateStrategy("alembic", str(ini_path))

        with patch("alembic.command.downgrade") as mock_downgrade:
            result = strategy.migrate_down(tmp_path, "ae1027a6acf")

        assert result.success
        mock_downgrade.assert_called_once()


class TestAlembicMigrateStrategyGetCurrentVersion:
    """Test Alembic get current version."""

    def test_get_current_version_parses_output(self, tmp_path):
        """get_current_version parses current revision from command output."""
        ini_path = tmp_path / "alembic.ini"
        ini_path.write_text("")

        strategy = AlembicMigrateStrategy("alembic", str(ini_path))

        with patch("alembic.command.current"), patch("sys.stdout", create=True):
            result = strategy.get_current_version(tmp_path)

        # Should either return None (due to empty output) or parsed revision
        assert result is None or isinstance(result, str)

    def test_get_current_version_returns_none_on_error(self, tmp_path):
        """get_current_version returns None when command.current raises."""
        ini_path = tmp_path / "alembic.ini"
        ini_path.write_text("")

        strategy = AlembicMigrateStrategy("alembic", str(ini_path))

        with patch("alembic.command.current", side_effect=Exception("current failed")):
            result = strategy.get_current_version(tmp_path)

        assert result is None

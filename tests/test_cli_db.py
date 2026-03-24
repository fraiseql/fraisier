"""Tests for CLI database commands."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_config():
    """Mock get_config to return a config with database settings."""
    config = MagicMock()
    config.get_fraise.return_value = {"type": "api", "description": "Test API"}
    config.get_fraise_environment.return_value = {
        "type": "api",
        "app_path": "/var/www/api",
        "database": {
            "name": "mydb",
            "strategy": "migrate",
            "confiture_config": "confiture.yaml",
            "template_prefix": "template_",
        },
    }
    config._config = {"backup": {}}
    config.deployment = MagicMock()
    config.deployment.get_strategy.return_value = "migrate"
    config.list_fraises_detailed.return_value = []
    with patch("fraisier.cli.main.get_config", return_value=config):
        yield config


class TestDbReset:
    """Tests for db reset command."""

    def test_db_reset_calls_reset_from_template(self, runner, mock_config):
        """db reset calls reset_from_template with correct args."""
        result_mock = MagicMock(success=True, template_name="template_mydb")
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.dbops.templates.reset_from_template", return_value=result_mock
            ) as mock_reset,
        ):
            result = runner.invoke(main, ["db", "reset", "my_api", "-e", "production"])

        assert result.exit_code == 0
        mock_reset.assert_called_once_with("mydb", prefix="template_")

    def test_db_reset_failure_exits_1(self, runner, mock_config):
        """db reset failure exits with error."""
        result_mock = MagicMock(success=False, error="template not found")
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.dbops.templates.reset_from_template",
                return_value=result_mock,
            ),
        ):
            result = runner.invoke(main, ["db", "reset", "my_api", "-e", "production"])

        assert result.exit_code == 1
        assert "failed" in result.output.lower()

    def test_db_reset_skips_external_db(self, runner, mock_config):
        """db reset skips when external_db is true."""
        with patch("fraisier.dbops.guard.is_external_db", return_value=True):
            result = runner.invoke(main, ["db", "reset", "my_api", "-e", "production"])

        assert result.exit_code == 0
        assert "external_db" in result.output.lower()

    def test_db_reset_unknown_fraise_exits_1(self, runner, mock_config):
        """db reset with unknown fraise/env exits with error."""
        mock_config.get_fraise.return_value = None

        result = runner.invoke(main, ["db", "reset", "nope", "-e", "production"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()


class TestDbMigrate:
    """Tests for db migrate command."""

    def test_db_migrate_calls_confiture_migrate(self, runner, mock_config):
        """db migrate calls confiture_migrate with correct args."""
        result_mock = MagicMock(success=True, migration_count=3)
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.dbops.confiture.confiture_migrate", return_value=result_mock
            ) as mock_migrate,
        ):
            result = runner.invoke(
                main, ["db", "migrate", "my_api", "-e", "production"]
            )

        assert result.exit_code == 0
        assert "3 applied" in result.output
        mock_migrate.assert_called_once_with(
            config_path="confiture.yaml",
            cwd="/var/www/api",
            direction="up",
        )

    def test_db_migrate_down(self, runner, mock_config):
        """db migrate -d down passes direction correctly."""
        result_mock = MagicMock(success=True, migration_count=1)
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.dbops.confiture.confiture_migrate", return_value=result_mock
            ) as mock_migrate,
        ):
            result = runner.invoke(
                main, ["db", "migrate", "my_api", "-e", "production", "-d", "down"]
            )

        assert result.exit_code == 0
        mock_migrate.assert_called_once_with(
            config_path="confiture.yaml",
            cwd="/var/www/api",
            direction="down",
        )

    def test_db_migrate_failure_exits_1(self, runner, mock_config):
        """db migrate failure exits with error."""
        result_mock = MagicMock(success=False, error="syntax error in migration")
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.dbops.confiture.confiture_migrate",
                return_value=result_mock,
            ),
        ):
            result = runner.invoke(
                main, ["db", "migrate", "my_api", "-e", "production"]
            )

        assert result.exit_code == 1
        assert "failed" in result.output.lower()

    def test_db_migrate_skips_external_db(self, runner, mock_config):
        """db migrate skips when external_db is true."""
        with patch("fraisier.dbops.guard.is_external_db", return_value=True):
            result = runner.invoke(
                main, ["db", "migrate", "my_api", "-e", "production"]
            )

        assert result.exit_code == 0
        assert "external_db" in result.output.lower()


class TestDbBuild:
    """Tests for db build command."""

    def test_db_build_calls_confiture_build(self, runner, mock_config):
        """db build calls confiture_build with correct args."""
        result_mock = MagicMock(success=True, migration_count=5)
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.dbops.confiture.confiture_build", return_value=result_mock
            ) as mock_build,
        ):
            result = runner.invoke(main, ["db", "build", "my_api", "-e", "production"])

        assert result.exit_code == 0
        assert "5 migrations" in result.output
        mock_build.assert_called_once_with(
            config_path="confiture.yaml",
            cwd="/var/www/api",
            rebuild=False,
        )

    def test_db_build_with_rebuild(self, runner, mock_config):
        """db build --rebuild passes rebuild=True."""
        result_mock = MagicMock(success=True, migration_count=5)
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.dbops.confiture.confiture_build", return_value=result_mock
            ) as mock_build,
        ):
            result = runner.invoke(
                main, ["db", "build", "my_api", "-e", "production", "--rebuild"]
            )

        assert result.exit_code == 0
        mock_build.assert_called_once_with(
            config_path="confiture.yaml",
            cwd="/var/www/api",
            rebuild=True,
        )


class TestBackup:
    """Tests for backup command."""

    def test_backup_calls_run_backup(self, runner, mock_config):
        """backup calls run_backup with correct args."""
        result_mock = MagicMock(success=True, backup_path="/backup/mydb.sql.zst")
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch("fraisier.dbops.backup.check_disk_space", return_value=True),
            patch(
                "fraisier.dbops.backup.run_backup", return_value=result_mock
            ) as mock_backup,
        ):
            result = runner.invoke(main, ["backup", "my_api", "-e", "production"])

        assert result.exit_code == 0
        mock_backup.assert_called_once_with(
            db_name="mydb",
            output_dir="/backup",
            compression="zstd:9",
            mode="full",
            excluded_tables=[],
        )

    def test_backup_insufficient_disk_space_exits_1(self, runner, mock_config):
        """backup exits 1 when disk space is insufficient."""
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch("fraisier.dbops.backup.check_disk_space", return_value=False),
        ):
            result = runner.invoke(main, ["backup", "my_api", "-e", "production"])

        assert result.exit_code == 1
        assert "disk space" in result.output.lower()

    def test_backup_failure_exits_1(self, runner, mock_config):
        """backup failure exits with error."""
        result_mock = MagicMock(success=False, error="pg_dump not found")
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch("fraisier.dbops.backup.check_disk_space", return_value=True),
            patch("fraisier.dbops.backup.run_backup", return_value=result_mock),
        ):
            result = runner.invoke(main, ["backup", "my_api", "-e", "production"])

        assert result.exit_code == 1
        assert "failed" in result.output.lower()

    def test_backup_slim_mode(self, runner, mock_config):
        """backup --mode slim passes excluded tables."""
        mock_config._config = {
            "backup": {
                "slim": {"excluded_tables": ["logs", "events"]},
            }
        }
        result_mock = MagicMock(success=True, backup_path="/backup/mydb.sql.zst")
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch("fraisier.dbops.backup.check_disk_space", return_value=True),
            patch(
                "fraisier.dbops.backup.run_backup", return_value=result_mock
            ) as mock_backup,
        ):
            result = runner.invoke(
                main, ["backup", "my_api", "-e", "production", "--mode", "slim"]
            )

        assert result.exit_code == 0
        mock_backup.assert_called_once_with(
            db_name="mydb",
            output_dir="/backup",
            compression="zstd:9",
            mode="slim",
            excluded_tables=["logs", "events"],
        )

"""Tests for CLI database commands."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main


@pytest.fixture
def runner():
    return CliRunner()


_ADMIN_URL = "postgresql://postgres@localhost:5432/postgres"
_APP_URL = "postgresql://app@localhost:5432/mydb"


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
            "admin_url": _ADMIN_URL,
            "database_url": _APP_URL,
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
        mock_reset.assert_called_once_with(
            "mydb", prefix="template_", connection_url=_ADMIN_URL
        )

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
            database_url=_APP_URL,
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
            database_url=_APP_URL,
            compression="zstd:9",
            mode="slim",
            excluded_tables=["logs", "events"],
        )


class TestDbRestore:
    """Tests for db restore command."""

    @pytest.fixture
    def restore_config(self):
        """Mock config with restore settings."""
        config = MagicMock()
        config.get_fraise.return_value = {"type": "api", "description": "Test API"}
        config.get_fraise_environment.return_value = {
            "type": "api",
            "app_path": "/var/www/api",
            "systemd_service": "api.staging.service",
            "database": {
                "name": "mydb_staging",
                "strategy": "restore_migrate",
                "admin_url": _ADMIN_URL,
                "confiture_config": "confiture.yaml",
                "restore": {
                    "backup_dir": "/backup/production",
                    "backup_pattern": "*.dump",
                    "max_age_hours": 48.0,
                    "create_template": True,
                    "min_tables": 100,
                },
            },
        }
        config._config = {"backup": {}}
        config.deployment = MagicMock()
        config.deployment.get_strategy.return_value = "restore_migrate"
        config.list_fraises_detailed.return_value = []
        with patch("fraisier.cli.main.get_config", return_value=config):
            yield config

    def test_db_restore_success(self, runner, restore_config):
        """db restore calls strategy.execute and reports success."""
        result_mock = MagicMock(success=True, migrations_applied=3)
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.strategies.RestoreMigrateStrategy.execute",
                return_value=result_mock,
            ) as mock_execute,
            patch("fraisier.systemd.SystemdServiceManager"),
        ):
            result = runner.invoke(main, ["db", "restore", "my_api", "staging"])

        assert result.exit_code == 0
        assert "3 migration(s) applied" in result.output
        mock_execute.assert_called_once()

    def test_db_restore_failure_exits_1(self, runner, restore_config):
        """db restore failure exits with error."""
        result_mock = MagicMock(success=False, errors=["pg_restore failed"])
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.strategies.RestoreMigrateStrategy.execute",
                return_value=result_mock,
            ),
            patch("fraisier.systemd.SystemdServiceManager"),
        ):
            result = runner.invoke(main, ["db", "restore", "my_api", "staging"])

        assert result.exit_code == 1
        assert "failed" in result.output.lower()

    def test_db_restore_database_error_exits_1(self, runner, restore_config):
        """db restore with DatabaseError exits 1 and restarts service."""
        from fraisier.errors import DatabaseError

        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.strategies.RestoreMigrateStrategy.execute",
                side_effect=DatabaseError("no backup found"),
            ),
            patch("fraisier.systemd.SystemdServiceManager") as mock_svc_mgr_class,
        ):
            mock_svc_mgr = MagicMock()
            mock_svc_mgr_class.return_value = mock_svc_mgr

            result = runner.invoke(main, ["db", "restore", "my_api", "staging"])

        assert result.exit_code == 1
        assert "failed" in result.output.lower()
        # Check that restart was called even on error
        mock_svc_mgr.restart.assert_called_once_with("api.staging.service")

    def test_db_restore_dry_run_no_execute(self, runner, restore_config):
        """--dry-run prints plan without calling execute."""
        from pathlib import Path as _Path

        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.dbops.restore.find_latest_backup",
                return_value=_Path("/backup/production/mydb_20260407.dump"),
            ),
            patch("fraisier.dbops.restore.validate_backup_age", return_value=True),
            patch("fraisier.strategies.RestoreMigrateStrategy.execute") as mock_execute,
            patch("fraisier.systemd.SystemdServiceManager"),
        ):
            result = runner.invoke(
                main, ["db", "restore", "my_api", "staging", "--dry-run"]
            )

        assert result.exit_code == 0
        assert "dry-run" in result.output.lower()
        mock_execute.assert_not_called()

    def test_db_restore_from_backup_passed_to_config(self, runner, restore_config):
        """--from-backup passes path to RestoreConfig.backup_path."""
        from pathlib import Path as _Path

        result_mock = MagicMock(success=True, migrations_applied=1)
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.strategies.RestoreMigrateStrategy.execute",
                return_value=result_mock,
            ),
            patch("fraisier.systemd.SystemdServiceManager"),
            patch("fraisier.runners.LocalRunner"),
        ):
            # Create a temp backup file for testing
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as f:
                backup_path = f.name

            try:
                result = runner.invoke(
                    main,
                    [
                        "db",
                        "restore",
                        "my_api",
                        "staging",
                        "--from-backup",
                        backup_path,
                    ],
                )

                assert result.exit_code == 0
                assert "1 migration(s) applied" in result.output
            finally:
                from pathlib import Path

                Path(backup_path).unlink()

    def test_db_restore_skips_external_db(self, runner, restore_config):
        """db restore skips when external_db is true."""
        with patch("fraisier.dbops.guard.is_external_db", return_value=True):
            result = runner.invoke(main, ["db", "restore", "my_api", "staging"])

        assert result.exit_code == 0
        assert "external_db" in result.output.lower()

    def test_db_restore_unknown_fraise_exits_1(self, runner, restore_config):
        """db restore with unknown fraise/env exits with error."""
        restore_config.get_fraise_environment.return_value = None

        result = runner.invoke(main, ["db", "restore", "nope", "staging"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_db_restore_missing_restore_config_exits_1(self, runner, restore_config):
        """db restore exits 1 if restore config is missing."""
        restore_config.get_fraise_environment.return_value = {
            "type": "api",
            "app_path": "/var/www/api",
            "database": {
                "name": "mydb",
                "strategy": "migrate",
                "confiture_config": "confiture.yaml",
                # No 'restore' key
            },
        }

        result = runner.invoke(main, ["db", "restore", "my_api", "staging"])

        assert result.exit_code == 1
        assert "no 'restore' config" in result.output.lower()

    def test_db_restore_no_service_restart(self, runner, restore_config):
        """--no-service-restart prevents service stop/restart."""
        result_mock = MagicMock(success=True, migrations_applied=1)
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.strategies.RestoreMigrateStrategy.execute",
                return_value=result_mock,
            ),
            patch("fraisier.systemd.SystemdServiceManager") as mock_svc_mgr_class,
            patch("fraisier.runners.LocalRunner"),
        ):
            mock_svc_mgr = MagicMock()
            mock_svc_mgr_class.return_value = mock_svc_mgr

            result = runner.invoke(
                main,
                ["db", "restore", "my_api", "staging", "--no-service-restart"],
            )

        assert result.exit_code == 0
        # SystemdServiceManager should not be instantiated
        mock_svc_mgr_class.assert_not_called()
        mock_svc_mgr.stop.assert_not_called()
        mock_svc_mgr.restart.assert_not_called()

    def test_db_restore_resolves_confiture_config_relative_to_app_path(
        self, runner, restore_config
    ):
        """Relative confiture_config is resolved against app_path."""
        from pathlib import Path as _Path

        result_mock = MagicMock(success=True, migrations_applied=0)
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.strategies.RestoreMigrateStrategy.execute",
                return_value=result_mock,
            ) as mock_execute,
            patch("fraisier.systemd.SystemdServiceManager"),
        ):
            result = runner.invoke(main, ["db", "restore", "my_api", "staging"])

        assert result.exit_code == 0
        # confiture_config arg should be resolved to app_path / confiture.yaml
        call_args = mock_execute.call_args
        confiture_arg = call_args[0][0]
        assert confiture_arg == _Path("/var/www/api/confiture.yaml")

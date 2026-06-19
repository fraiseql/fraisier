"""Tests for fraisier db preflight CLI command and --skip-preflight on db restore."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.dbops.preflight import MigrationCheck, MigrationPreflightResult

_ADMIN_URL = "postgresql://postgres@localhost:5432/postgres"
_BACKUP_FILE = Path("/backups/latest.dump")

_P_PREFLIGHT = "fraisier.dbops.preflight.run_migration_preflight"
_P_FIND_BACKUP = "fraisier.dbops.restore.find_latest_backup"
_P_VALIDATE_AGE = "fraisier.dbops.restore.validate_backup_age"


def _passing_result() -> MigrationPreflightResult:
    return MigrationPreflightResult(
        migrations=[
            MigrationCheck(version="001", name="add_table", passed=True, time_ms=120),
            MigrationCheck(version="002", name="fix_func", passed=True, time_ms=80),
        ],
        schema_extraction_ms=1200,
        total_ms=1500,
    )


def _failing_result() -> MigrationPreflightResult:
    return MigrationPreflightResult(
        migrations=[
            MigrationCheck(version="001", name="add_table", passed=True, time_ms=120),
            MigrationCheck(
                version="002",
                name="fix_func",
                passed=False,
                error="cannot change name of input parameter",
            ),
        ],
        schema_extraction_ms=1200,
        total_ms=1500,
    )


def _false_positive_result() -> MigrationPreflightResult:
    """Earlier non-transactional migration skipped; later dependent fails (#250)."""
    return MigrationPreflightResult(
        migrations=[
            MigrationCheck(
                version="20240101120000",
                name="add_idx_concurrently",
                passed=False,
                skipped=True,
                skipped_reason="non-transactional: cannot run inside SAVEPOINT",
            ),
            MigrationCheck(
                version="20240101130000",
                name="dependent_view",
                passed=False,
                error='relation "public.widgets" does not exist',
            ),
        ],
        schema_extraction_ms=1200,
        total_ms=1500,
    )


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.get_fraise.return_value = {"type": "api"}
    config.get_fraise_environment.return_value = {
        "app_path": "/var/www/api",
        "database": {
            "name": "mydb",
            "admin_url": _ADMIN_URL,
            "confiture_config": "confiture.yaml",
            "restore": {
                "backup_dir": "/backups",
                "backup_pattern": "*.dump",
                "max_age_hours": 48.0,
            },
        },
    }
    with patch("fraisier.cli.main.get_config", return_value=config):
        yield config


# ---------------------------------------------------------------------------
# db preflight — help
# ---------------------------------------------------------------------------


class TestDbPreflightHelp:
    def test_help_exits_zero(self, runner):
        result = runner.invoke(main, ["db", "preflight", "--help"])
        assert result.exit_code == 0

    def test_help_mentions_env_option(self, runner):
        result = runner.invoke(main, ["db", "preflight", "--help"])
        assert "--env" in result.output or "-e" in result.output

    def test_help_mentions_format_option(self, runner):
        result = runner.invoke(main, ["db", "preflight", "--help"])
        assert "--format" in result.output


# ---------------------------------------------------------------------------
# db preflight — exit codes
# ---------------------------------------------------------------------------


class TestDbPreflightExitCodes:
    def test_exits_zero_when_all_pass(self, runner, mock_config):
        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=_passing_result()),
        ):
            result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        assert result.exit_code == 0

    def test_exits_one_when_migration_fails(self, runner, mock_config):
        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=_failing_result()),
        ):
            result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        assert result.exit_code == 1

    def test_exits_two_when_no_restore_config(self, runner, mock_config):
        mock_config.get_fraise_environment.return_value = {
            "app_path": "/var/www/api",
            "database": {
                "name": "mydb",
                "admin_url": _ADMIN_URL,
            },
        }
        result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        assert result.exit_code == 2

    def test_exits_one_when_missing_admin_url(self, runner, mock_config):
        mock_config.get_fraise_environment.return_value = {
            "app_path": "/var/www/api",
            "database": {
                "name": "mydb",
                "restore": {"backup_dir": "/backups"},
            },
        }
        result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        assert result.exit_code == 1

    def test_exits_one_when_no_backup_found(self, runner, mock_config):
        with patch(_P_FIND_BACKUP, return_value=None):
            result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        assert result.exit_code == 1

    def test_exits_one_when_fraise_not_found(self, runner, mock_config):
        mock_config.get_fraise.return_value = None
        mock_config.get_fraise_environment.return_value = None
        result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# db preflight — text output
# ---------------------------------------------------------------------------


class TestDbPreflightTextOutput:
    def test_shows_migration_versions(self, runner, mock_config):
        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=_passing_result()),
        ):
            result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        assert "001" in result.output
        assert "002" in result.output

    def test_shows_migration_names(self, runner, mock_config):
        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=_passing_result()),
        ):
            result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        assert "add_table" in result.output

    def test_shows_passed_summary(self, runner, mock_config):
        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=_passing_result()),
        ):
            result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        assert "passed" in result.output.lower()

    def test_shows_failure_error_message(self, runner, mock_config):
        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=_failing_result()),
        ):
            result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        assert "cannot change name" in result.output

    def test_shows_failure_count(self, runner, mock_config):
        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=_failing_result()),
        ):
            result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        # "1 of 2" migrations would fail
        assert "1" in result.output and "2" in result.output

    def test_genuine_failure_mentions_skip_preflight_footer(self, runner, mock_config):
        """A real failure surfaces --skip-preflight as the emergency escape hatch."""
        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=_failing_result()),
        ):
            result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        assert "--skip-preflight" in result.output
        # No false-positive diagnostic for a genuine failure.
        assert "non-transactional" not in result.output

    def test_false_positive_shows_diagnostic_note(self, runner, mock_config):
        """Issue #250 false-alarm signature surfaces the non-transactional note."""
        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=_false_positive_result()),
        ):
            result = runner.invoke(main, ["db", "preflight", "myapp", "-e", "staging"])
        assert result.exit_code == 1
        assert "non-transactional" in result.output
        assert "--skip-preflight" in result.output


# ---------------------------------------------------------------------------
# db preflight — JSON output
# ---------------------------------------------------------------------------


class TestDbPreflightJsonOutput:
    def _invoke_json(self, runner, mock_config, preflight_result):
        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=preflight_result),
        ):
            return runner.invoke(
                main,
                ["db", "preflight", "myapp", "-e", "staging", "--format", "json"],
            )

    def test_json_exits_zero_on_pass(self, runner, mock_config):
        result = self._invoke_json(runner, mock_config, _passing_result())
        assert result.exit_code == 0

    def test_json_exits_one_on_failure(self, runner, mock_config):
        result = self._invoke_json(runner, mock_config, _failing_result())
        assert result.exit_code == 1

    def test_json_output_is_valid_json(self, runner, mock_config):
        result = self._invoke_json(runner, mock_config, _passing_result())
        # Should not raise
        json.loads(result.output)

    def test_json_has_all_passed_field(self, runner, mock_config):
        result = self._invoke_json(runner, mock_config, _passing_result())
        data = json.loads(result.output)
        assert "all_passed" in data
        assert data["all_passed"] is True

    def test_json_has_migrations_list(self, runner, mock_config):
        result = self._invoke_json(runner, mock_config, _passing_result())
        data = json.loads(result.output)
        assert "migrations" in data
        assert len(data["migrations"]) == 2

    def test_json_has_total_ms(self, runner, mock_config):
        result = self._invoke_json(runner, mock_config, _passing_result())
        data = json.loads(result.output)
        assert "total_ms" in data
        assert data["total_ms"] == 1500

    def test_json_migration_has_required_fields(self, runner, mock_config):
        result = self._invoke_json(runner, mock_config, _passing_result())
        data = json.loads(result.output)
        m = data["migrations"][0]
        assert m["version"] == "001"
        assert m["name"] == "add_table"
        assert m["passed"] is True

    def test_json_failure_includes_error(self, runner, mock_config):
        result = self._invoke_json(runner, mock_config, _failing_result())
        data = json.loads(result.output)
        failed = next(m for m in data["migrations"] if not m["passed"])
        assert "cannot change name" in (failed["error"] or "")

    def test_json_has_suspected_false_positive_count(self, runner, mock_config):
        result = self._invoke_json(runner, mock_config, _false_positive_result())
        data = json.loads(result.output)
        assert data["suspected_false_positive_count"] == 1

    def test_json_suspected_count_zero_for_genuine_failure(self, runner, mock_config):
        result = self._invoke_json(runner, mock_config, _failing_result())
        data = json.loads(result.output)
        assert data["suspected_false_positive_count"] == 0


# ---------------------------------------------------------------------------
# db restore — --skip-preflight flag
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config_with_restore():
    config = MagicMock()
    config.get_fraise.return_value = {"type": "api"}
    config.get_fraise_environment.return_value = {
        "app_path": "/var/www/api",
        "database": {
            "name": "mydb",
            "admin_url": _ADMIN_URL,
            "confiture_config": "confiture.yaml",
            "restore": {
                "backup_dir": "/backups",
                "backup_pattern": "*.dump",
                "max_age_hours": 48.0,
                "create_template": False,
                "min_tables": 0,
            },
        },
    }
    config._config = {}
    with patch("fraisier.cli.main.get_config", return_value=config):
        yield config


class TestDbRestoreSkipPreflight:
    # Local import in db_restore: `from fraisier.strategies import RestoreMigrateStrategy`
    # binds the name at call time from fraisier.strategies — patch at source.
    _P_STRATEGY = "fraisier.strategies.RestoreMigrateStrategy"
    _P_CONFIG = "fraisier.strategies.RestoreConfig"

    def test_skip_preflight_flag_accepted(self, runner, mock_config_with_restore):
        """--skip-preflight flag is accepted without error."""
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(self._P_STRATEGY) as mock_cls,
            patch(self._P_CONFIG),
        ):
            mock_cls.return_value.execute.return_value = MagicMock(
                success=True, migrations_applied=1
            )
            result = runner.invoke(
                main,
                ["db", "restore", "myapp", "staging", "--skip-preflight"],
            )
        # Should not fail with "no such option"
        assert "no such option" not in result.output.lower()

    def test_skip_preflight_passed_to_execute(self, runner, mock_config_with_restore):
        """execute() is called with skip_preflight=True when flag is set."""
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(self._P_STRATEGY) as mock_cls,
            patch(self._P_CONFIG),
        ):
            mock_cls.return_value.execute.return_value = MagicMock(
                success=True, migrations_applied=1
            )
            runner.invoke(
                main,
                ["db", "restore", "myapp", "staging", "--skip-preflight"],
            )
        execute_call = mock_cls.return_value.execute.call_args
        assert execute_call is not None
        assert execute_call.kwargs.get("skip_preflight") is True

    def test_without_flag_execute_called_with_false(
        self, runner, mock_config_with_restore
    ):
        """Without --skip-preflight, execute() gets skip_preflight=False."""
        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(self._P_STRATEGY) as mock_cls,
            patch(self._P_CONFIG),
        ):
            mock_cls.return_value.execute.return_value = MagicMock(
                success=True, migrations_applied=1
            )
            runner.invoke(main, ["db", "restore", "myapp", "staging"])
        execute_call = mock_cls.return_value.execute.call_args
        assert execute_call is not None
        assert execute_call.kwargs.get("skip_preflight") is False

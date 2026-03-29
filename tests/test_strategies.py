"""Tests for deployment strategies (v0.3 confiture Python API)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.confiture import (
    IrreversibleMigrationError,
    MigrationError,
    MigrationResult,
)
from fraisier.strategies import (
    MigrateStrategy,
    RebuildStrategy,
    RestoreMigrateStrategy,
    get_strategy,
)

CONFIG = Path("confiture.yaml")
MDIR = Path("db/migrations")


# ---------------------------------------------------------------------------
# MigrateStrategy
# ---------------------------------------------------------------------------


class TestMigrateStrategy:
    """Production strategy: preflight → migrate up."""

    @patch("fraisier.strategies.migrate_up")
    @patch("fraisier.strategies.preflight")
    def test_execute_success(self, mock_preflight, mock_up):
        mock_up.return_value = MigrationResult(
            success=True, steps_applied=3, execution_time_ms=120
        )

        strategy = MigrateStrategy()
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        assert result.migrations_applied == 3
        mock_preflight.assert_called_once_with(
            CONFIG, migrations_dir=MDIR, allow_irreversible=False
        )
        mock_up.assert_called_once_with(
            CONFIG,
            migrations_dir=MDIR,
            pre_migrate_verify=False,
            require_reversible=True,
        )

    @patch("fraisier.strategies.preflight")
    def test_execute_preflight_blocks_irreversible(self, mock_preflight):
        mock_preflight.side_effect = IrreversibleMigrationError("V003 has no down")

        strategy = MigrateStrategy()
        with pytest.raises(IrreversibleMigrationError):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies.migrate_up")
    @patch("fraisier.strategies.preflight")
    def test_execute_allows_irreversible(self, mock_preflight, mock_up):
        mock_up.return_value = MigrationResult(success=True, steps_applied=1)

        strategy = MigrateStrategy()
        result = strategy.execute(CONFIG, migrations_dir=MDIR, allow_irreversible=True)

        assert result.success
        mock_preflight.assert_called_once_with(
            CONFIG, migrations_dir=MDIR, allow_irreversible=True
        )

    @patch("fraisier.strategies.migrate_up")
    @patch("fraisier.strategies.preflight")
    def test_execute_migration_failure_raises(self, mock_preflight, mock_up):
        mock_up.side_effect = MigrationError("syntax error")

        strategy = MigrateStrategy()
        with pytest.raises(MigrationError):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies.migrate_down")
    def test_rollback_success(self, mock_down):
        mock_down.return_value = MigrationResult(success=True, steps_applied=2)

        strategy = MigrateStrategy()
        result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=2)

        assert result.success
        assert result.migrations_applied == 2
        mock_down.assert_called_once_with(CONFIG, migrations_dir=MDIR, steps=2)

    @patch("fraisier.strategies.migrate_down")
    def test_rollback_failure(self, mock_down):
        mock_down.return_value = MigrationResult(
            success=False, errors=["constraint violation"]
        )

        strategy = MigrateStrategy()
        result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=1)

        assert not result.success
        assert "constraint violation" in result.errors

    @patch("fraisier.strategies.migrate_up")
    @patch("fraisier.strategies.preflight")
    def test_execute_with_pre_migrate_verify(self, mock_preflight, mock_up):
        mock_up.return_value = MigrationResult(success=True, steps_applied=1)

        strategy = MigrateStrategy()
        result = strategy.execute(CONFIG, migrations_dir=MDIR, pre_migrate_verify=True)

        assert result.success
        mock_up.assert_called_once_with(
            CONFIG,
            migrations_dir=MDIR,
            pre_migrate_verify=True,
            require_reversible=True,
        )


# ---------------------------------------------------------------------------
# RebuildStrategy
# ---------------------------------------------------------------------------


def _mock_rebuild_deps(mock_env_validate, mock_migrator_cls):
    """Set up common mocks for RebuildStrategy tests."""
    mock_env = MagicMock()
    mock_env.name = "dev"
    mock_env.database_url = "postgresql:///mydb"
    mock_env_validate.return_value = mock_env

    mock_session = MagicMock()
    mock_migrator_cls.from_config.return_value.__enter__ = MagicMock(
        return_value=mock_session
    )
    mock_migrator_cls.from_config.return_value.__exit__ = MagicMock(return_value=False)
    return mock_env, mock_session


class TestRebuildStrategy:
    """Development strategy: build SQL + psql apply + reinit."""

    @patch("fraisier.strategies.subprocess.run")
    @patch("confiture.core.migrator.Migrator")
    @patch("confiture.core.builder.SchemaBuilder")
    @patch("confiture.config.environment.Environment.model_validate")
    @patch("yaml.safe_load", return_value={})
    @patch("pathlib.Path.read_text", return_value="")
    def test_execute_builds_and_applies_via_psql(
        self,
        mock_read_text,
        mock_yaml_load,
        mock_env_validate,
        mock_builder_cls,
        mock_migrator_cls,
        mock_subprocess_run,
    ):
        _, mock_session = _mock_rebuild_deps(mock_env_validate, mock_migrator_cls)
        mock_builder = MagicMock()
        mock_builder_cls.return_value = mock_builder

        strategy = RebuildStrategy()
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        mock_builder_cls.assert_called_once_with(env="dev")
        mock_builder.build.assert_called_once()

        # Two psql calls: drop schemas + apply file.
        assert mock_subprocess_run.call_count == 2
        drop_call, apply_call = mock_subprocess_run.call_args_list
        assert "-c" in drop_call[0][0]
        assert "DROP SCHEMA" in drop_call[0][0][-1]
        assert "-f" in apply_call[0][0]

        mock_session.reinit.assert_called_once()

    @patch("fraisier.strategies.subprocess.run")
    @patch("confiture.core.migrator.Migrator")
    @patch("confiture.core.builder.SchemaBuilder")
    @patch("confiture.config.environment.Environment.model_validate")
    @patch("yaml.safe_load", return_value={})
    @patch("pathlib.Path.read_text", return_value="")
    def test_execute_cleans_up_temp_file_on_psql_failure(
        self,
        mock_read_text,
        mock_yaml_load,
        mock_env_validate,
        mock_builder_cls,
        mock_migrator_cls,
        mock_subprocess_run,
    ):
        import subprocess as sp

        _mock_rebuild_deps(mock_env_validate, mock_migrator_cls)
        mock_builder_cls.return_value = MagicMock()

        # First psql call (drop) succeeds, second (apply) fails.
        mock_subprocess_run.side_effect = [
            MagicMock(returncode=0),
            sp.CalledProcessError(1, "psql"),
        ]

        strategy = RebuildStrategy()
        with pytest.raises(sp.CalledProcessError):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies.subprocess.run")
    @patch("confiture.core.migrator.Migrator")
    @patch("confiture.core.builder.SchemaBuilder")
    @patch("confiture.config.environment.Environment.model_validate")
    @patch("yaml.safe_load", return_value={})
    @patch("pathlib.Path.read_text", return_value="")
    def test_rollback_calls_execute(
        self,
        mock_read_text,
        mock_yaml_load,
        mock_env_validate,
        mock_builder_cls,
        mock_migrator_cls,
        mock_subprocess_run,
    ):
        _, mock_session = _mock_rebuild_deps(mock_env_validate, mock_migrator_cls)
        mock_builder_cls.return_value = MagicMock()

        strategy = RebuildStrategy()
        result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=5)

        assert result.success
        mock_session.reinit.assert_called_once()


# ---------------------------------------------------------------------------
# RestoreMigrateStrategy
# ---------------------------------------------------------------------------


class TestRestoreMigrateStrategy:
    """Staging strategy: restore backup, then migrate up."""

    @patch("fraisier.strategies.migrate_up")
    @patch("fraisier.strategies.subprocess.run")
    def test_execute_restore_then_migrate(self, mock_run, mock_up):
        mock_run.return_value = MagicMock(returncode=0)
        mock_up.return_value = MigrationResult(success=True, steps_applied=2)

        strategy = RestoreMigrateStrategy("pg_restore -d staging /backup/latest.dump")
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        assert result.migrations_applied == 2
        # Must use list (shlex.split) with shell=False
        mock_run.assert_called_once_with(
            ["pg_restore", "-d", "staging", "/backup/latest.dump"],
            check=True,
        )
        mock_up.assert_called_once()

    def test_rejects_shell_metacharacters_in_restore_command(self):
        """RestoreMigrateStrategy must reject commands with shell metacharacters."""
        with pytest.raises(ValueError, match="metacharacter"):
            RestoreMigrateStrategy("pg_restore dump.sql; rm -rf /")

    @patch("fraisier.strategies.migrate_up")
    @patch("fraisier.strategies.subprocess.run")
    def test_execute_uses_list_not_shell(self, mock_run, mock_up):
        """subprocess.run must receive a list, not a string with shell=True."""
        mock_run.return_value = MagicMock(returncode=0)
        mock_up.return_value = MigrationResult(success=True, steps_applied=0)

        strategy = RestoreMigrateStrategy("pg_restore dump.sql")
        strategy.execute(CONFIG, migrations_dir=MDIR)

        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert isinstance(cmd, list)
        assert "shell" not in call_args[1]

    @patch("fraisier.strategies.subprocess.run")
    def test_execute_restore_failure_raises(self, mock_run):
        import subprocess

        mock_run.side_effect = subprocess.CalledProcessError(1, "pg_restore")

        strategy = RestoreMigrateStrategy("pg_restore -d staging /bad")
        with pytest.raises(subprocess.CalledProcessError):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies.migrate_down")
    def test_rollback_calls_migrate_down(self, mock_down):
        mock_down.return_value = MigrationResult(success=True, steps_applied=2)

        strategy = RestoreMigrateStrategy("pg_restore -d staging /backup/latest.dump")
        result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=2)

        assert result.success
        assert result.migrations_applied == 2


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestGetStrategy:
    """Test strategy factory."""

    def test_migrate(self):
        assert isinstance(get_strategy("migrate"), MigrateStrategy)

    def test_rebuild(self):
        assert isinstance(get_strategy("rebuild"), RebuildStrategy)

    def test_restore_migrate(self):
        s = get_strategy("restore_migrate", restore_command="pg_restore -d db /b.dump")
        assert isinstance(s, RestoreMigrateStrategy)

    def test_restore_migrate_requires_command(self):
        with pytest.raises(ValueError, match="restore_command"):
            get_strategy("restore_migrate")

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            get_strategy("canary")

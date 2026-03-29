"""Tests for deployment strategies (v0.3 confiture Python API)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.confiture import (
    IrreversibleMigrationError,
    MigrationError,
    MigrationResult,
)
from fraisier.dbops.restore import RestoreResult
from fraisier.dbops.templates import TemplateResult
from fraisier.strategies import (
    MigrateStrategy,
    RebuildStrategy,
    RestoreConfig,
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
            CONFIG,
            migrations_dir=MDIR,
            allow_irreversible=False,
            database_url=None,
        )
        mock_up.assert_called_once_with(
            CONFIG,
            migrations_dir=MDIR,
            pre_migrate_verify=False,
            require_reversible=True,
            database_url=None,
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
            CONFIG,
            migrations_dir=MDIR,
            allow_irreversible=True,
            database_url=None,
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
        mock_down.assert_called_once_with(
            CONFIG, migrations_dir=MDIR, steps=2, database_url=None
        )

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
    def test_execute_passes_database_url_override(self, mock_preflight, mock_up):
        mock_up.return_value = MigrationResult(success=True, steps_applied=1)
        url = "postgresql:///mydb?host=/var/run/postgresql"

        strategy = MigrateStrategy()
        result = strategy.execute(CONFIG, migrations_dir=MDIR, database_url=url)

        assert result.success
        mock_preflight.assert_called_once_with(
            CONFIG,
            migrations_dir=MDIR,
            allow_irreversible=False,
            database_url=url,
        )
        mock_up.assert_called_once_with(
            CONFIG,
            migrations_dir=MDIR,
            pre_migrate_verify=False,
            require_reversible=True,
            database_url=url,
        )

    @patch("fraisier.strategies.migrate_down")
    def test_rollback_passes_database_url_override(self, mock_down):
        mock_down.return_value = MigrationResult(success=True, steps_applied=1)
        url = "postgresql:///mydb?host=/var/run/postgresql"

        strategy = MigrateStrategy()
        result = strategy.rollback(
            CONFIG, migrations_dir=MDIR, steps=1, database_url=url
        )

        assert result.success
        mock_down.assert_called_once_with(
            CONFIG, migrations_dir=MDIR, steps=1, database_url=url
        )

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
            database_url=None,
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
    @patch("yaml.safe_load", return_value={"database_url": "postgresql:///original"})
    @patch("pathlib.Path.read_text", return_value="")
    def test_execute_with_database_url_override(
        self,
        mock_read_text,
        mock_yaml_load,
        mock_env_validate,
        mock_builder_cls,
        mock_migrator_cls,
        mock_subprocess_run,
    ):
        _mock_rebuild_deps(mock_env_validate, mock_migrator_cls)
        mock_builder_cls.return_value = MagicMock()
        url = "postgresql:///override?host=/var/run/postgresql"

        strategy = RebuildStrategy()
        result = strategy.execute(CONFIG, migrations_dir=MDIR, database_url=url)

        assert result.success
        # Verify the raw dict was patched before model_validate
        call_args = mock_env_validate.call_args[0][0]
        assert call_args["database_url"] == url

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


def _make_config(**overrides) -> RestoreConfig:
    """Create a RestoreConfig with sensible defaults for tests."""
    defaults = {
        "db_name": "staging_db",
        "backup_dir": Path("/backup/production"),
    }
    defaults.update(overrides)
    return RestoreConfig(**defaults)


class TestRestoreMigrateStrategy:
    """Staging strategy: full backup restore lifecycle."""

    # -- Construction / validation --

    def test_init_validates_db_name(self):
        with pytest.raises(ValueError, match="database name"):
            RestoreMigrateStrategy(_make_config(db_name="bad;name"))

    def test_init_validates_target_owner(self):
        with pytest.raises(ValueError, match="target owner"):
            RestoreMigrateStrategy(_make_config(target_owner="bad;owner"))

    def test_init_validates_template_name(self):
        with pytest.raises(ValueError, match="template name"):
            RestoreMigrateStrategy(_make_config(template_name="bad;tmpl"))

    def test_init_accepts_valid_config(self):
        strategy = RestoreMigrateStrategy(_make_config(target_owner="app_user"))
        assert strategy._config.db_name == "staging_db"

    # -- Execute lifecycle --

    @patch("fraisier.strategies.migrate_up")
    @patch("fraisier.dbops.restore.validate_table_count")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db")
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_full_lifecycle(
        self,
        mock_find,
        mock_age,
        mock_term,
        mock_drop,
        mock_create,
        mock_restore,
        mock_table,
        mock_up,
    ):
        backup = Path("/backup/production/db_2026.dump")
        mock_find.return_value = backup
        mock_create.return_value = (0, "", "")
        mock_restore.return_value = RestoreResult(success=True)
        mock_table.return_value = (True, 350)
        mock_up.return_value = MigrationResult(success=True, steps_applied=5)

        cfg = _make_config(
            target_owner="app_user",
            min_tables=300,
        )
        strategy = RestoreMigrateStrategy(cfg)
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        assert result.migrations_applied == 5

        mock_find.assert_called_once_with(cfg.backup_dir, pattern=cfg.backup_pattern)
        mock_age.assert_called_once_with(backup, max_age_hours=48.0)
        mock_term.assert_called_once_with("staging_db")
        mock_drop.assert_called_once_with("staging_db")
        mock_create.assert_called_once_with("staging_db")
        mock_restore.assert_called_once_with(
            backup_path=str(backup),
            db_name="staging_db",
            db_owner="app_user",
        )
        mock_table.assert_called_once_with("staging_db", min_threshold=300)
        mock_up.assert_called_once()

    @patch("fraisier.dbops.restore.find_latest_backup", return_value=None)
    def test_execute_no_backup_found_raises(self, mock_find):
        from fraisier.errors import DatabaseError

        strategy = RestoreMigrateStrategy(_make_config())
        with pytest.raises(DatabaseError, match="No backup"):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.dbops.restore.validate_backup_age", return_value=False)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_backup_too_old_raises(self, mock_find, mock_age):
        from fraisier.errors import DatabaseError

        mock_find.return_value = Path("/backup/old.dump")
        strategy = RestoreMigrateStrategy(_make_config())
        with pytest.raises(DatabaseError, match="older than"):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db", return_value=(0, "", ""))
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_restore_failure_raises(
        self, mock_find, mock_age, mock_term, mock_drop, mock_create, mock_restore
    ):
        from fraisier.errors import DatabaseError

        mock_find.return_value = Path("/backup/db.dump")
        mock_restore.return_value = RestoreResult(success=False, error="corrupt file")

        strategy = RestoreMigrateStrategy(_make_config())
        with pytest.raises(DatabaseError, match="pg_restore failed"):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies.migrate_up")
    @patch("fraisier.dbops.restore.validate_table_count", return_value=(False, 10))
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db", return_value=(0, "", ""))
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_table_count_below_threshold_raises(
        self,
        mock_find,
        mock_age,
        mock_term,
        mock_drop,
        mock_create,
        mock_restore,
        mock_table,
        mock_up,
    ):
        from fraisier.errors import DatabaseError

        mock_find.return_value = Path("/backup/db.dump")
        mock_restore.return_value = RestoreResult(success=True)
        mock_up.return_value = MigrationResult(success=True, steps_applied=0)

        strategy = RestoreMigrateStrategy(_make_config(min_tables=300))
        with pytest.raises(DatabaseError, match="Table count validation failed"):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies.migrate_up")
    @patch("fraisier.dbops.restore.validate_table_count")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db", return_value=(0, "", ""))
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_skips_table_validation_when_min_tables_zero(
        self,
        mock_find,
        mock_age,
        mock_term,
        mock_drop,
        mock_create,
        mock_restore,
        mock_table,
        mock_up,
    ):
        mock_find.return_value = Path("/backup/db.dump")
        mock_restore.return_value = RestoreResult(success=True)
        mock_up.return_value = MigrationResult(success=True, steps_applied=0)

        strategy = RestoreMigrateStrategy(_make_config(min_tables=0))
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        mock_table.assert_not_called()

    @patch("fraisier.strategies.migrate_up")
    @patch("fraisier.dbops.operations.create_db")
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_creates_template_when_configured(
        self,
        mock_find,
        mock_age,
        mock_restore,
        mock_term,
        mock_drop,
        mock_create,
        mock_up,
    ):
        mock_find.return_value = Path("/backup/db.dump")
        mock_restore.return_value = RestoreResult(success=True)
        mock_create.return_value = (0, "", "")
        mock_up.return_value = MigrationResult(success=True, steps_applied=1)

        strategy = RestoreMigrateStrategy(
            _make_config(create_template=True, template_name="staging_tmpl")
        )
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        # create_db called twice: once for db, once for template
        assert mock_create.call_count == 2
        template_call = mock_create.call_args_list[1]
        assert template_call[0] == ("staging_tmpl",)
        assert template_call[1] == {"template": "staging_db"}

    # -- Rollback --

    @patch("fraisier.strategies.migrate_up")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db", return_value=(0, "", ""))
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_passes_database_url_to_migrate_up(
        self,
        mock_find,
        mock_age,
        mock_term,
        mock_drop,
        mock_create,
        mock_restore,
        mock_up,
    ):
        mock_find.return_value = Path("/backup/db.dump")
        mock_restore.return_value = RestoreResult(success=True)
        mock_up.return_value = MigrationResult(success=True, steps_applied=1)
        url = "postgresql:///staging?host=/var/run/postgresql"

        strategy = RestoreMigrateStrategy(_make_config())
        result = strategy.execute(CONFIG, migrations_dir=MDIR, database_url=url)

        assert result.success
        mock_up.assert_called_once_with(
            CONFIG, migrations_dir=MDIR, database_url=url
        )

    @patch("fraisier.strategies.migrate_down")
    def test_rollback_passes_database_url_to_migrate_down(self, mock_down):
        mock_down.return_value = MigrationResult(success=True, steps_applied=1)
        url = "postgresql:///staging?host=/var/run/postgresql"

        strategy = RestoreMigrateStrategy(_make_config())
        result = strategy.rollback(
            CONFIG, migrations_dir=MDIR, steps=1, database_url=url
        )

        assert result.success
        mock_down.assert_called_once_with(
            CONFIG, migrations_dir=MDIR, steps=1, database_url=url
        )

    @patch("fraisier.strategies.migrate_down")
    def test_rollback_without_template_calls_migrate_down(self, mock_down):
        mock_down.return_value = MigrationResult(success=True, steps_applied=2)

        strategy = RestoreMigrateStrategy(_make_config())
        result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=2)

        assert result.success
        assert result.migrations_applied == 2

    @patch("fraisier.dbops.operations.create_db", return_value=(0, "", ""))
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    def test_rollback_with_template_uses_template(
        self, mock_term, mock_drop, mock_create
    ):
        strategy = RestoreMigrateStrategy(
            _make_config(create_template=True, template_name="staging_tmpl")
        )
        result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=2)

        assert result.success
        # Should drop staging_db then create from template
        mock_create.assert_called_once_with("staging_db", template="staging_tmpl")

    @patch("fraisier.dbops.templates.reset_from_template")
    def test_rollback_with_default_template_name(self, mock_reset):
        mock_reset.return_value = TemplateResult(
            success=True, template_name="template_staging_db"
        )
        strategy = RestoreMigrateStrategy(_make_config(create_template=True))
        result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=2)

        assert result.success
        mock_reset.assert_called_once_with("staging_db", prefix="template_")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_SAMPLE_RESTORE_CONFIG = {
    "backup_dir": "/backup/production",
    "backup_pattern": "*.dump",
    "max_age_hours": 24,
    "target_owner": "app_user",
}


class TestGetStrategy:
    """Test strategy factory."""

    def test_migrate(self):
        assert isinstance(get_strategy("migrate"), MigrateStrategy)

    def test_rebuild(self):
        assert isinstance(get_strategy("rebuild"), RebuildStrategy)

    def test_restore_migrate(self):
        s = get_strategy(
            "restore_migrate",
            db_name="staging_db",
            restore_config=_SAMPLE_RESTORE_CONFIG,
        )
        assert isinstance(s, RestoreMigrateStrategy)

    def test_restore_migrate_requires_config(self):
        with pytest.raises(ValueError, match="restore_config"):
            get_strategy("restore_migrate", db_name="staging_db")

    def test_restore_migrate_requires_db_name(self):
        with pytest.raises(ValueError, match="db_name"):
            get_strategy("restore_migrate", restore_config=_SAMPLE_RESTORE_CONFIG)

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            get_strategy("canary")

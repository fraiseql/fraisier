"""Tests for deployment strategies (v0.3 confiture Python API)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from fraisier.dbops.confiture import (
    IrreversibleMigrationError,
    MigrationError,
    MigrationResult,
)
from fraisier.dbops.restore import RestoreResult
from fraisier.dbops.templates import TemplateResult
from fraisier.strategies import (
    DjangoMigrateStrategy,
    MigrateStrategy,
    RebuildStrategy,
    RestoreConfig,
    RestoreMigrateStrategy,
    get_strategy,
)
from fraisier.strategies._base import StrategyResult

CONFIG = Path("confiture.yaml")
MDIR = Path("db/migrations")
_ADMIN_URL = "postgresql://postgres:pass@localhost:5432/postgres"


# ---------------------------------------------------------------------------
# MigrateStrategy
# ---------------------------------------------------------------------------


class TestMigrateStrategy:
    """Production strategy: preflight → migrate up."""

    @patch("fraisier.strategies._core.migrate_up")
    @patch("fraisier.strategies._core.preflight")
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
            hooks_config=None,
        )

    @patch("fraisier.strategies._core.preflight")
    def test_execute_preflight_blocks_irreversible(self, mock_preflight):
        mock_preflight.side_effect = IrreversibleMigrationError("V003 has no down")

        strategy = MigrateStrategy()
        with pytest.raises(IrreversibleMigrationError):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies._core.migrate_up")
    @patch("fraisier.strategies._core.preflight")
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

    @patch("fraisier.strategies._core.migrate_up")
    @patch("fraisier.strategies._core.preflight")
    def test_execute_migration_failure_raises(self, mock_preflight, mock_up):
        mock_up.side_effect = MigrationError("syntax error")

        strategy = MigrateStrategy()
        with pytest.raises(MigrationError):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies._core.migrate_down")
    def test_rollback_success(self, mock_down):
        mock_down.return_value = MigrationResult(success=True, steps_applied=2)

        strategy = MigrateStrategy()
        result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=2)

        assert result.success
        assert result.migrations_applied == 2
        mock_down.assert_called_once_with(
            CONFIG, migrations_dir=MDIR, steps=2, database_url=None, hooks_config=None
        )

    @patch("fraisier.strategies._core.migrate_down")
    def test_rollback_failure(self, mock_down):
        mock_down.return_value = MigrationResult(
            success=False, errors=["constraint violation"]
        )

        strategy = MigrateStrategy()
        result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=1)

        assert not result.success
        assert "constraint violation" in result.errors

    @patch("fraisier.strategies._core.migrate_up")
    @patch("fraisier.strategies._core.preflight")
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
            hooks_config=None,
        )

    @patch("fraisier.strategies._core.migrate_down")
    def test_rollback_passes_database_url_override(self, mock_down):
        mock_down.return_value = MigrationResult(success=True, steps_applied=1)
        url = "postgresql:///mydb?host=/var/run/postgresql"

        strategy = MigrateStrategy()
        result = strategy.rollback(
            CONFIG, migrations_dir=MDIR, steps=1, database_url=url
        )

        assert result.success
        mock_down.assert_called_once_with(
            CONFIG, migrations_dir=MDIR, steps=1, database_url=url, hooks_config=None
        )

    @patch("fraisier.strategies._core.migrate_up")
    @patch("fraisier.strategies._core.preflight")
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
            hooks_config=None,
        )


class TestRebuildStrategyValidation:
    """RebuildStrategy constructor validation (no DB needed).

    Execute/rollback behavior is covered by integration tests in
    tests/integration/test_strategies_integration.py.
    """

    def test_init_validates_required_roles(self):
        with pytest.raises(ValueError, match="required role"):
            RebuildStrategy(required_roles=["bad;role"])

    def test_init_accepts_valid_roles(self):
        strategy = RebuildStrategy(required_roles=["app_core", "app_admin"])
        assert strategy._required_roles == ["app_core", "app_admin"]

    def test_init_defaults_to_empty_roles(self):
        strategy = RebuildStrategy()
        assert strategy._required_roles == []

    def test_init_create_template_false_by_default(self):
        strategy = RebuildStrategy()
        assert strategy._create_template is False

    def test_init_create_template_stores_flag(self):
        strategy = RebuildStrategy(create_template=True)
        assert strategy._create_template is True

    def test_init_template_name_none_by_default(self):
        strategy = RebuildStrategy()
        assert strategy._template_name is None

    def test_init_template_name_validates_identifier(self):
        with pytest.raises(ValueError, match="template name"):
            RebuildStrategy(template_name="bad;name")

    def test_init_template_name_accepts_valid(self):
        strategy = RebuildStrategy(template_name="template_myapp")
        assert strategy._template_name == "template_myapp"

    def test_resolved_template_name_default(self):
        strategy = RebuildStrategy()
        assert strategy._resolved_template_name("myapp") == "template_myapp"

    def test_resolved_template_name_explicit(self):
        strategy = RebuildStrategy(template_name="snapshot_myapp")
        assert strategy._resolved_template_name("myapp") == "snapshot_myapp"


class TestProvisionRolesIdentifierValidation:
    """Defense-in-depth: ``_provision_roles`` must validate every identifier
    it interpolates into raw SQL, even when callers have already validated
    earlier (e.g. ``__init__``).  The constructor path covers ``role`` only;
    ``db_owner`` flows in directly from caller arguments and is otherwise
    unchecked before reaching the f-string at strategies.py:270.
    """

    _ADVERSARIAL_NAMES = (
        "ro'le; DROP TABLE pg_roles;--",
        "my-role",
        "role with space",
        "$(whoami)",
        "`id`",
        "",
        "1starts_with_digit",
    )

    @pytest.mark.parametrize("bad_owner", _ADVERSARIAL_NAMES)
    @patch("fraisier.strategies._core.run_psql")
    def test_provision_roles_rejects_unsafe_owner(self, mock_run_psql, bad_owner):
        mock_run_psql.return_value = (0, "", "")
        strategy = RebuildStrategy(required_roles=["app_core"])
        with pytest.raises(ValueError):
            strategy._provision_roles(
                db_name="appdb",
                db_owner=bad_owner,
                connection_url=_ADMIN_URL,
            )
        mock_run_psql.assert_not_called()

    @pytest.mark.parametrize("bad_role", _ADVERSARIAL_NAMES)
    @patch("fraisier.strategies._core.run_psql")
    def test_provision_roles_rejects_unsafe_role_defense_in_depth(
        self, mock_run_psql, bad_role
    ):
        # Bypass __init__ validation to simulate a regression elsewhere; the
        # method itself must still refuse to build SQL with a tainted role.
        mock_run_psql.return_value = (0, "", "")
        strategy = RebuildStrategy()
        strategy._required_roles = [bad_role]
        with pytest.raises(ValueError):
            strategy._provision_roles(
                db_name="appdb",
                db_owner="app_owner",
                connection_url=_ADMIN_URL,
            )
        mock_run_psql.assert_not_called()

    @patch("fraisier.strategies._core.run_psql")
    def test_provision_roles_accepts_safe_names(self, mock_run_psql):
        mock_run_psql.return_value = (0, "", "")
        strategy = RebuildStrategy(required_roles=["readonly", "app_user"])
        strategy._provision_roles(
            db_name="appdb",
            db_owner="app_owner",
            connection_url=_ADMIN_URL,
        )
        # 2 roles x (CREATE ROLE + GRANT) = 4 psql invocations
        assert mock_run_psql.call_count == 4

    @patch("fraisier.strategies._core.run_psql")
    def test_provision_roles_accepts_safe_names_no_owner(self, mock_run_psql):
        mock_run_psql.return_value = (0, "", "")
        strategy = RebuildStrategy(required_roles=["readonly"])
        strategy._provision_roles(
            db_name="appdb",
            db_owner=None,
            connection_url=_ADMIN_URL,
        )
        # No owner → only CREATE ROLE, no GRANT
        assert mock_run_psql.call_count == 1


class TestDjangoGetLatestVersionExceptionNarrowing:
    """``DjangoMigrateStrategy.get_latest_version`` iterates over Django app
    configs. The inner loop must skip apps whose migration discovery raises
    *expected* errors (missing module → ImportError) but propagate
    *unexpected* errors so the outer warning handler can record them — the
    current bare ``except Exception: continue`` silently masks real bugs.

    These tests use ``sys.modules`` patching so they run without Django
    installed (the package is an optional framework integration).
    """

    @staticmethod
    def _patch_django(get_module_side_effect, *, n_apps=1):
        from unittest.mock import MagicMock as _MM

        fake_apps_attr = _MM()
        fake_apps_attr.get_app_configs.return_value = [_MM() for _ in range(n_apps)]
        fake_django_apps = _MM()
        fake_django_apps.apps = fake_apps_attr

        fake_migrations_attr = _MM()
        fake_migrations_attr.get_migration_module.side_effect = get_module_side_effect
        fake_django_db = _MM()
        fake_django_db.migrations = fake_migrations_attr

        return patch.dict(
            "sys.modules",
            {
                "django": _MM(),
                "django.apps": fake_django_apps,
                "django.db": fake_django_db,
            },
        )

    def test_unexpected_exception_in_inner_loop_reaches_outer_warning(self):
        strategy = DjangoMigrateStrategy("settings")
        strategy.app_label = None  # force the multi-app branch

        with (
            self._patch_django(ValueError("real bug")),
            patch("fraisier.strategies._django.log") as mock_log,
        ):
            result = strategy.get_latest_version(Path("/fake"))

        assert result is None
        mock_log.warning.assert_called()

    def test_import_error_in_inner_loop_is_skipped_silently(self):
        strategy = DjangoMigrateStrategy("settings")
        strategy.app_label = None

        with (
            self._patch_django(ImportError("no migrations")),
            patch("fraisier.strategies._django.log") as mock_log,
        ):
            result = strategy.get_latest_version(Path("/fake"))

        assert result is None
        mock_log.warning.assert_not_called()


# ---------------------------------------------------------------------------
# RestoreMigrateStrategy
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> RestoreConfig:
    """Create a RestoreConfig with sensible defaults for tests.

    Preflight is disabled by default here — the restore lifecycle tests focus
    on backup/restore/migrate steps. Preflight behaviour is covered separately
    in test_restore_strategy_preflight.py.
    """
    from fraisier.config.schema import PreflightConfig

    defaults = {
        "db_name": "staging_db",
        "backup_dir": Path("/backup/production"),
        "preflight": PreflightConfig(enabled=False),
    }
    defaults.update(overrides)
    return RestoreConfig(**defaults)  # ty: ignore[invalid-argument-type]


def _make_strategy(
    config: RestoreConfig | None = None,
    *,
    admin_url: str = _ADMIN_URL,
    service_manager=None,
    service_name: str | None = None,
    **config_overrides,
) -> RestoreMigrateStrategy:
    """Build a RestoreMigrateStrategy with a default admin_url for tests."""
    if config is None:
        config = _make_config(**config_overrides)
    return RestoreMigrateStrategy(
        config,
        admin_url=admin_url,
        service_manager=service_manager,
        service_name=service_name,
    )


class TestRestoreMigrateStrategy:
    """Staging strategy: full backup restore lifecycle."""

    # -- Construction / validation --

    def test_init_validates_db_name(self):
        with pytest.raises(ValueError, match="database name"):
            _make_strategy(db_name="bad;name")

    def test_init_validates_target_owner(self):
        with pytest.raises(ValueError, match="target owner"):
            _make_strategy(target_owner="bad;owner")

    def test_init_validates_template_name(self):
        with pytest.raises(ValueError, match="template name"):
            _make_strategy(template_name="bad;tmpl")

    def test_init_accepts_valid_config(self):
        strategy = _make_strategy(target_owner="app_user")
        assert strategy._config.db_name == "staging_db"

    def test_restore_config_jobs_default(self):
        cfg = _make_config()
        assert cfg.jobs == 1

    def test_restore_config_jobs_custom(self):
        cfg = _make_config(jobs=4)
        assert cfg.jobs == 4

    def test_restore_config_preferred_compression_default(self):
        cfg = _make_config()
        assert cfg.preferred_compression is None

    def test_restore_config_preferred_compression_custom(self):
        cfg = _make_config(preferred_compression="lz4")
        assert cfg.preferred_compression == "lz4"

    # -- Compression preference passthrough --

    @patch("fraisier.strategies._restore.migrate_up")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db")
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_passes_preferred_compression(
        self,
        mock_find,
        mock_age,
        mock_term,
        mock_drop,
        mock_create,
        mock_restore,
        mock_up,
    ):
        mock_find.return_value = Path("/backup/db_lz4.dump")
        mock_drop.return_value = (0, "", "")
        mock_create.return_value = (0, "", "")
        mock_restore.return_value = RestoreResult(success=True, duration_seconds=0.1)
        mock_up.return_value = MigrationResult(success=True, steps_applied=0)

        strategy = _make_strategy(preferred_compression="lz4")
        strategy.execute(CONFIG, migrations_dir=MDIR)

        _, kwargs = mock_find.call_args
        assert kwargs["preferred_compression"] == "lz4"

    # -- Jobs passthrough --

    @patch("fraisier.strategies._restore.migrate_up")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db")
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_passes_jobs_to_restore(
        self,
        mock_find,
        mock_age,
        mock_term,
        mock_drop,
        mock_create,
        mock_restore,
        mock_up,
    ):
        mock_find.return_value = Path("/backup/latest.dump")
        mock_drop.return_value = (0, "", "")
        mock_create.return_value = (0, "", "")
        mock_restore.return_value = RestoreResult(success=True)
        mock_up.return_value = MigrationResult(success=True, steps_applied=0)

        strategy = _make_strategy(jobs=4)
        strategy.execute(CONFIG, migrations_dir=MDIR)

        _, kwargs = mock_restore.call_args
        assert kwargs["jobs"] == 4

    # -- Timing observability --

    def test_strategy_result_timing_defaults(self):
        result = StrategyResult(success=True)
        assert result.restore_duration_seconds == 0.0
        assert result.migration_duration_seconds == 0.0
        assert result.total_duration_seconds == 0.0

    @patch("fraisier.strategies._restore.migrate_up")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db")
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_returns_timing(
        self,
        mock_find,
        mock_age,
        mock_term,
        mock_drop,
        mock_create,
        mock_restore,
        mock_up,
    ):
        mock_find.return_value = Path("/backup/latest.dump")
        mock_drop.return_value = (0, "", "")
        mock_create.return_value = (0, "", "")
        mock_restore.return_value = RestoreResult(success=True, duration_seconds=1.5)
        mock_up.return_value = MigrationResult(success=True, steps_applied=0)

        strategy = _make_strategy()
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        assert result.restore_duration_seconds > 0
        assert result.total_duration_seconds > 0

    # -- Execute lifecycle --

    @patch("fraisier.strategies._restore.migrate_up")
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
        mock_drop.return_value = (0, "", "")
        mock_create.return_value = (0, "", "")
        mock_restore.return_value = RestoreResult(success=True)
        mock_table.return_value = (True, 350)
        mock_up.return_value = MigrationResult(success=True, steps_applied=5)

        cfg = _make_config(
            target_owner="app_user",
            min_tables=300,
        )
        strategy = _make_strategy(cfg)
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        assert result.migrations_applied == 5

        mock_find.assert_called_once_with(
            cfg.backup_dir,
            pattern=cfg.backup_pattern,
            preferred_compression=None,
        )
        mock_age.assert_called_once_with(backup, max_age_hours=48.0)
        mock_term.assert_called_once_with("staging_db", connection_url=_ADMIN_URL)
        mock_drop.assert_called_once_with(
            "staging_db", force=True, connection_url=_ADMIN_URL
        )
        mock_create.assert_called_once_with("staging_db", connection_url=_ADMIN_URL)
        mock_restore.assert_called_once_with(
            backup_path=str(backup),
            db_name="staging_db",
            db_owner="app_user",
            connection_url=_ADMIN_URL,
            jobs=1,
        )
        mock_table.assert_called_once_with(
            "staging_db", min_threshold=300, connection_url=_ADMIN_URL
        )
        mock_up.assert_called_once()

    @patch("fraisier.strategies._restore.migrate_up")
    @patch("fraisier.dbops.restore.validate_table_count")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db")
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_with_service_manager(
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
        from unittest.mock import MagicMock

        backup = Path("/backup/production/db_2026.dump")
        mock_find.return_value = backup
        mock_drop.return_value = (0, "", "")
        mock_create.return_value = (0, "", "")
        mock_restore.return_value = RestoreResult(success=True)
        mock_table.return_value = (True, 350)
        mock_up.return_value = MigrationResult(success=True, steps_applied=5)

        # Mock ServiceManager
        mock_svc_mgr = MagicMock()
        cfg = _make_config(target_owner="app_user", min_tables=300)
        strategy = _make_strategy(
            cfg, service_manager=mock_svc_mgr, service_name="test_svc"
        )
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        assert result.migrations_applied == 5

        # Verify service stop and wait_stopped were called
        mock_svc_mgr.stop.assert_called_once_with("test_svc")
        mock_svc_mgr.wait_stopped.assert_called_once_with("test_svc")
        # Verify service start was called last
        mock_svc_mgr.start.assert_called_once_with("test_svc")

    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_service_stop_failure_raises(self, mock_find, mock_age):
        """If service stop fails, execution should stop immediately."""
        from unittest.mock import MagicMock

        from fraisier.errors import DatabaseError
        from fraisier.service_managers.base import ServiceManagerError

        mock_find.return_value = Path("/backup/db.dump")

        mock_svc_mgr = MagicMock()
        mock_svc_mgr.stop.side_effect = ServiceManagerError("Unit not found")

        strategy = _make_strategy(
            service_manager=mock_svc_mgr, service_name="api.service"
        )
        with pytest.raises(DatabaseError, match=r"Failed to stop service api\.service"):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies._restore.migrate_up")
    @patch("fraisier.dbops.restore.validate_table_count")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db", return_value=(0, "", ""))
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_service_start_failure_raises(
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
        """If service start fails, execution should stop immediately."""
        from unittest.mock import MagicMock

        from fraisier.errors import DatabaseError
        from fraisier.service_managers.base import ServiceManagerError

        backup = Path("/backup/production/db_2026.dump")
        mock_find.return_value = backup
        mock_drop.return_value = (0, "", "")
        mock_create.return_value = (0, "", "")
        mock_restore.return_value = RestoreResult(success=True)
        mock_table.return_value = (True, 350)
        mock_up.return_value = MigrationResult(success=True, steps_applied=5)

        mock_svc_mgr = MagicMock()
        mock_svc_mgr.start.side_effect = ServiceManagerError("Unit not loaded")

        strategy = _make_strategy(
            service_manager=mock_svc_mgr, service_name="api.service", min_tables=300
        )
        with pytest.raises(
            DatabaseError, match=r"Failed to start service api\.service"
        ):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies._restore.migrate_up")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db", return_value=(0, "", ""))
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_without_service_name_skips_service_ops(
        self,
        mock_find,
        mock_age,
        mock_term,
        mock_drop,
        mock_create,
        mock_restore,
        mock_up,
    ):
        """When service_name is None, service operations should be skipped."""
        mock_find.return_value = Path("/backup/db.dump")
        mock_drop.return_value = (0, "", "")
        mock_create.return_value = (0, "", "")
        mock_restore.return_value = RestoreResult(success=True)
        mock_up.return_value = MigrationResult(success=True, steps_applied=1)

        strategy = _make_strategy(service_name=None)  # No service
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        # These should not be patched/called since service_name is None

    # -- Rollback --

    @patch("fraisier.strategies._restore.migrate_up")
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
        mock_drop.return_value = (0, "", "")
        mock_find.return_value = Path("/backup/db.dump")
        mock_restore.return_value = RestoreResult(success=True)
        mock_up.return_value = MigrationResult(success=True, steps_applied=1)
        url = "postgresql:///staging?host=/var/run/postgresql"

        strategy = _make_strategy()
        result = strategy.execute(CONFIG, migrations_dir=MDIR, database_url=url)

        assert result.success
        mock_up.assert_called_once_with(
            CONFIG, migrations_dir=MDIR, database_url=url, hooks_config=None
        )

    @patch("fraisier.strategies._restore.migrate_up")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db")
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_with_create_template_drops_template_with_clear_flag(
        self,
        mock_find,
        mock_age,
        mock_term,
        mock_drop,
        mock_create,
        mock_restore,
        mock_up,
    ):
        """create_template path passes clear_template_flag=True on the template drop (#200)."""
        mock_find.return_value = Path("/backup/db.dump")
        mock_drop.return_value = (0, "", "")
        mock_create.return_value = (0, "", "")
        mock_restore.return_value = RestoreResult(success=True)
        mock_up.return_value = MigrationResult(success=True, steps_applied=0)

        strategy = _make_strategy(create_template=True, template_name="tpl_staging")
        strategy.execute(CONFIG, migrations_dir=MDIR)

        template_drop_calls = [
            call
            for call in mock_drop.call_args_list
            if call.args and call.args[0] == "tpl_staging"
        ]
        assert len(template_drop_calls) == 1
        assert template_drop_calls[0].kwargs.get("clear_template_flag") is True

    @patch("fraisier.strategies._restore.migrate_up")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db")
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_raises_when_template_drop_fails(
        self,
        mock_find,
        mock_age,
        mock_term,
        mock_drop,
        mock_create,
        mock_restore,
        mock_up,
    ):
        """Non-zero return from template drop_db surfaces as DatabaseError (#200)."""
        from fraisier.errors import DatabaseError

        mock_find.return_value = Path("/backup/db.dump")
        mock_create.return_value = (0, "", "")
        mock_restore.return_value = RestoreResult(success=True)
        mock_up.return_value = MigrationResult(success=True, steps_applied=0)

        def drop_side_effect(db, **_kw):
            if db == "tpl_staging":
                return (1, "", "permission denied")
            return (0, "", "")

        mock_drop.side_effect = drop_side_effect

        strategy = _make_strategy(create_template=True, template_name="tpl_staging")
        with pytest.raises(DatabaseError, match="Failed to drop template"):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies._restore.migrate_down")
    def test_rollback_passes_database_url_to_migrate_down(self, mock_down):
        mock_down.return_value = MigrationResult(success=True, steps_applied=1)
        url = "postgresql:///staging?host=/var/run/postgresql"

        strategy = _make_strategy()
        result = strategy.rollback(
            CONFIG, migrations_dir=MDIR, steps=1, database_url=url
        )

        assert result.success
        mock_down.assert_called_once_with(
            CONFIG, migrations_dir=MDIR, steps=1, database_url=url, hooks_config=None
        )

    @patch("fraisier.strategies._restore.migrate_down")
    def test_rollback_without_template_calls_migrate_down(self, mock_down):
        mock_down.return_value = MigrationResult(success=True, steps_applied=2)

        strategy = _make_strategy()
        result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=2)

        assert result.success
        assert result.migrations_applied == 2

    @patch("fraisier.dbops.operations.create_db", return_value=(0, "", ""))
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    def test_rollback_with_template_uses_template(
        self, mock_term, mock_drop, mock_create
    ):
        mock_drop.return_value = (0, "", "")
        strategy = _make_strategy(create_template=True, template_name="staging_tmpl")
        result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=2)

        assert result.success
        # Should drop staging_db then create from template
        mock_create.assert_called_once_with(
            "staging_db", template="staging_tmpl", connection_url=_ADMIN_URL
        )

    @patch("fraisier.dbops.templates.reset_from_template")
    def test_rollback_with_default_template_name(self, mock_reset):
        mock_reset.return_value = TemplateResult(
            success=True, template_name="template_staging_db"
        )
        strategy = _make_strategy(create_template=True)
        result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=2)

        assert result.success
        mock_reset.assert_called_once_with(
            "staging_db", prefix="template_", connection_url=_ADMIN_URL
        )


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

    def test_rebuild_with_required_roles(self):
        s = get_strategy("rebuild", required_roles=["app_core", "app_admin"])
        assert isinstance(s, RebuildStrategy)
        assert s._required_roles == ["app_core", "app_admin"]

    def test_rebuild_create_template_defaults_false(self):
        s = get_strategy("rebuild")
        assert s._create_template is False  # ty: ignore[unresolved-attribute]

    def test_rebuild_with_create_template(self):
        s = get_strategy("rebuild", create_template=True)
        assert isinstance(s, RebuildStrategy)
        assert s._create_template is True

    def test_rebuild_with_template_name(self):
        s = get_strategy("rebuild", create_template=True, template_name="snap_myapp")
        assert isinstance(s, RebuildStrategy)
        assert s._template_name == "snap_myapp"

    def test_rebuild_forwards_app_version(self):
        """get_strategy forwards app_version kwarg to RebuildStrategy."""
        s = get_strategy("rebuild", create_template=True, app_version="1.2.3")
        assert isinstance(s, RebuildStrategy)
        assert s._app_version == "1.2.3"

    def test_rebuild_app_version_default_is_none(self):
        """Without app_version kwarg, the strategy's _app_version is None."""
        s = get_strategy("rebuild")
        assert s._app_version is None  # ty: ignore[unresolved-attribute]

    def test_rebuild_empty_app_version_is_ignored(self):
        """Empty-string app_version is dropped so auto-discovery runs."""
        s = get_strategy("rebuild", create_template=True, app_version="")
        assert s._app_version is None  # ty: ignore[unresolved-attribute]

    def test_rebuild_invalid_app_version_raises(self):
        """Invalid app_version propagates ValueError (not swallowed by factory)."""
        with pytest.raises(ValueError, match="app_version"):
            get_strategy("rebuild", create_template=True, app_version="1!2.3.4")

    def test_restore_migrate(self):
        s = get_strategy(
            "restore_migrate",
            db_name="staging_db",
            restore_config=_SAMPLE_RESTORE_CONFIG,
            admin_url=_ADMIN_URL,
        )
        assert isinstance(s, RestoreMigrateStrategy)

    def test_restore_migrate_with_service_manager(self):
        from unittest.mock import MagicMock

        mock_svc_mgr = MagicMock()
        s = get_strategy(
            "restore_migrate",
            db_name="staging_db",
            restore_config=_SAMPLE_RESTORE_CONFIG,
            admin_url=_ADMIN_URL,
            service_manager=mock_svc_mgr,
            service_name="test_svc",
        )
        assert isinstance(s, RestoreMigrateStrategy)
        assert s._service_manager is mock_svc_mgr
        assert s._service_name == "test_svc"

    def test_restore_migrate_requires_config(self):
        with pytest.raises(ValueError, match="restore_config"):
            get_strategy("restore_migrate", db_name="staging_db")

    def test_restore_migrate_requires_db_name(self):
        with pytest.raises(ValueError, match="db_name"):
            get_strategy("restore_migrate", restore_config=_SAMPLE_RESTORE_CONFIG)

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown"):
            get_strategy("canary")

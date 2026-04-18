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
    """Create a RestoreConfig with sensible defaults for tests."""
    defaults = {
        "db_name": "staging_db",
        "backup_dir": Path("/backup/production"),
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

        mock_find.assert_called_once_with(cfg.backup_dir, pattern=cfg.backup_pattern)
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

        # Verify service manager calls
        mock_svc_mgr.stop.assert_called_once_with("test_svc")
        mock_svc_mgr.wait_stopped.assert_called_once_with("test_svc")
        mock_svc_mgr.start.assert_called_once_with("test_svc")

    @patch("fraisier.dbops.restore.find_latest_backup", return_value=None)
    def test_execute_no_backup_found_raises(self, mock_find):
        from fraisier.errors import DatabaseError

        strategy = _make_strategy()
        with pytest.raises(DatabaseError, match="No backup"):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.dbops.restore.validate_backup_age", return_value=False)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_backup_too_old_raises(self, mock_find, mock_age):
        from fraisier.errors import DatabaseError

        mock_find.return_value = Path("/backup/old.dump")
        strategy = _make_strategy()
        with pytest.raises(DatabaseError, match="older than"):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_drop_failure_raises(
        self, mock_find, mock_age, mock_term, mock_drop
    ):
        from fraisier.errors import DatabaseError

        mock_find.return_value = Path("/backup/db.dump")
        mock_drop.return_value = (1, "", 'database "staging_db" already exists')

        strategy = _make_strategy()
        with pytest.raises(DatabaseError, match="Failed to drop database staging_db"):
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

        mock_drop.return_value = (0, "", "")
        mock_find.return_value = Path("/backup/db.dump")
        mock_restore.return_value = RestoreResult(success=False, error="corrupt file")

        strategy = _make_strategy()
        with pytest.raises(DatabaseError, match="pg_restore failed"):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies._restore.migrate_up")
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

        mock_drop.return_value = (0, "", "")
        mock_find.return_value = Path("/backup/db.dump")
        mock_restore.return_value = RestoreResult(success=True)
        mock_up.return_value = MigrationResult(success=True, steps_applied=0)

        strategy = _make_strategy(min_tables=300)
        with pytest.raises(DatabaseError, match="Table count validation failed"):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies._restore.migrate_up")
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
        mock_drop.return_value = (0, "", "")
        mock_find.return_value = Path("/backup/db.dump")
        mock_restore.return_value = RestoreResult(success=True)
        mock_up.return_value = MigrationResult(success=True, steps_applied=0)

        strategy = _make_strategy(min_tables=0)
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        mock_table.assert_not_called()

    @patch("fraisier.strategies._restore.migrate_up")
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
        mock_drop.return_value = (0, "", "")
        mock_find.return_value = Path("/backup/db.dump")
        mock_restore.return_value = RestoreResult(success=True)
        mock_create.return_value = (0, "", "")
        mock_up.return_value = MigrationResult(success=True, steps_applied=1)

        strategy = _make_strategy(create_template=True, template_name="staging_tmpl")
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        # create_db called twice: once for db, once for template
        assert mock_create.call_count == 2
        template_call = mock_create.call_args_list[1]
        assert template_call[0] == ("staging_tmpl",)
        assert template_call[1] == {
            "template": "staging_db",
            "connection_url": _ADMIN_URL,
        }

    @patch("fraisier.strategies._restore.migrate_up")
    @patch("fraisier.dbops.restore.validate_table_count")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db", return_value=(0, "", ""))
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.operations.start_service")
    @patch("fraisier.dbops.operations.stop_service")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_stops_and_starts_service(
        self,
        mock_find,
        mock_age,
        mock_stop_svc,
        mock_start_svc,
        mock_term,
        mock_drop,
        mock_create,
        mock_restore,
        mock_table,
        mock_up,
    ):
        """Service should be stopped before DB operations and restarted after."""
        backup = Path("/backup/production/db_2026.dump")
        mock_find.return_value = backup
        mock_stop_svc.return_value = (0, "", "")
        mock_start_svc.return_value = (0, "", "")
        mock_drop.return_value = (0, "", "")
        mock_create.return_value = (0, "", "")
        mock_restore.return_value = RestoreResult(success=True)
        mock_table.return_value = (True, 350)
        mock_up.return_value = MigrationResult(success=True, steps_applied=5)

        strategy = _make_strategy(service_name="api.service", min_tables=300)
        result = strategy.execute(CONFIG, migrations_dir=MDIR)

        assert result.success
        # Verify service stop was called first
        mock_stop_svc.assert_called_once_with("api.service")
        # Verify service start was called last
        mock_start_svc.assert_called_once_with("api.service")

    @patch("fraisier.dbops.operations.stop_service")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_service_stop_failure_raises(
        self, mock_find, mock_age, mock_stop_svc
    ):
        """If service stop fails, execution should stop immediately."""
        from fraisier.errors import DatabaseError

        mock_find.return_value = Path("/backup/db.dump")
        mock_stop_svc.return_value = (1, "", "Unit not found")

        strategy = _make_strategy(service_name="api.service")
        with pytest.raises(DatabaseError, match="Failed to stop service"):
            strategy.execute(CONFIG, migrations_dir=MDIR)

    @patch("fraisier.strategies._restore.migrate_up")
    @patch("fraisier.dbops.restore.restore_backup")
    @patch("fraisier.dbops.operations.create_db", return_value=(0, "", ""))
    @patch("fraisier.dbops.operations.drop_db")
    @patch("fraisier.dbops.operations.terminate_backends")
    @patch("fraisier.dbops.operations.start_service")
    @patch("fraisier.dbops.operations.stop_service")
    @patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
    @patch("fraisier.dbops.restore.find_latest_backup")
    def test_execute_service_start_failure_raises(
        self,
        mock_find,
        mock_age,
        mock_stop_svc,
        mock_start_svc,
        mock_term,
        mock_drop,
        mock_create,
        mock_restore,
        mock_up,
    ):
        """If service start fails after restore, execution should fail."""
        from fraisier.errors import DatabaseError

        mock_find.return_value = Path("/backup/db.dump")
        mock_stop_svc.return_value = (0, "", "")
        mock_drop.return_value = (0, "", "")
        mock_create.return_value = (0, "", "")
        mock_restore.return_value = RestoreResult(success=True)
        mock_start_svc.return_value = (1, "", "Connection refused")
        mock_up.return_value = MigrationResult(success=True, steps_applied=1)

        strategy = _make_strategy(service_name="api.service")
        with pytest.raises(DatabaseError, match="Failed to start service"):
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

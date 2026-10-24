"""#481: rollback when step 8 took no template.

Without a template the rollback is ``migrate down`` over the migrations this
run applied. Two things have to hold for that to happen:

- a failure *after* ``migrate up`` committed (the table floor, the empty-TVIEW
  check, the service start) must still say how many migrations applied. It used
  to raise a bare ``DatabaseError``, so the deployer saw 0 and rolled nothing
  back, template or not;
- the rollback must know what step 8 decided. ``auto`` decides per run, so the
  configuration alone no longer says whether a template exists.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.config.schema import PreflightConfig
from fraisier.dbops.confiture import MigrationResult
from fraisier.dbops.restore import RestoreResult
from fraisier.dbops.template_rule import TemplateDecision
from fraisier.deployers.base import DeploymentStatus
from fraisier.errors import DatabaseError
from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy

CONFIG = Path("confiture.yaml")
MDIR = Path("db/migrations")
_ADMIN_URL = "postgresql://admin@localhost/postgres"

_SKIP = TemplateDecision(take=False, reason="all 1 pending migration(s) …")
_TAKE = TemplateDecision(take=True, reason="002 has no down", forced_by=("002",))


def _strategy(**overrides) -> RestoreMigrateStrategy:
    config = RestoreConfig(
        db_name="staging_db",
        backup_dir=Path("/backup"),
        preflight=PreflightConfig(enabled=False),
        create_template=True,
        **overrides,
    )
    return RestoreMigrateStrategy(config, admin_url=_ADMIN_URL)


@pytest.fixture
def pipeline():
    """Steps 1-9 stubbed: a dump found and restored, one migration applied."""
    with (
        patch("fraisier.dbops.restore.find_latest_backup") as find,
        patch("fraisier.dbops.restore.validate_backup_age", return_value=True),
        patch("fraisier.dbops.operations.terminate_backends"),
        patch("fraisier.dbops.operations.drop_db", return_value=(0, "", "")),
        patch("fraisier.dbops.operations.create_db", return_value=(0, "", "")),
        patch("fraisier.dbops.restore.restore_backup") as restore,
        patch("fraisier.strategies._restore.migrate_up") as up,
        patch("fraisier.dbops.restore.validate_table_count") as count,
        patch.object(RestoreMigrateStrategy, "_check_tviews_not_empty"),
    ):
        find.return_value = Path("/backup/db.dump")
        restore.return_value = RestoreResult(success=True)
        up.return_value = MigrationResult(success=True, steps_applied=1)
        count.return_value = (True, 10)
        yield {"count": count}


class TestAFailureAfterMigrateSaysWhatApplied:
    def test_a_table_floor_failure_carries_the_applied_count(self, pipeline):
        pipeline["count"].return_value = (False, 3)
        strategy = _strategy(template="never", min_tables=99)

        with pytest.raises(DatabaseError, match="Table count") as exc_info:
            strategy.execute(CONFIG, migrations_dir=MDIR)

        assert exc_info.value.steps_applied == 1

    def test_an_empty_tview_failure_carries_the_applied_count(self, pipeline):
        strategy = _strategy(template="never")

        with (
            patch.object(
                RestoreMigrateStrategy,
                "_check_tviews_not_empty",
                side_effect=DatabaseError("1 pg_tviews TVIEW(s) are empty"),
            ),
            pytest.raises(DatabaseError) as exc_info,
        ):
            strategy.execute(CONFIG, migrations_dir=MDIR)

        assert exc_info.value.steps_applied == 1

    def test_a_service_start_failure_carries_the_applied_count(self, pipeline):
        manager = MagicMock()
        manager.start.side_effect = RuntimeError("unit failed")
        config = RestoreConfig(
            db_name="staging_db",
            backup_dir=Path("/backup"),
            preflight=PreflightConfig(enabled=False),
            template="never",
        )
        strategy = RestoreMigrateStrategy(
            config,
            admin_url=_ADMIN_URL,
            service_manager=manager,
            service_name="app.service",
        )

        with (
            patch.object(strategy, "_record_actuation_of"),
            pytest.raises(DatabaseError, match="Failed to start") as exc_info,
        ):
            strategy.execute(CONFIG, migrations_dir=MDIR)

        assert exc_info.value.steps_applied == 1

    def test_a_failure_before_migrate_still_says_nothing(self):
        """The count is only claimed once migrations could have applied."""
        assert DatabaseError("boom").steps_applied is None


class TestTheRollbackFollowsStep8:
    """The decision is the run's, not the configuration's."""

    def _executed(self, decision: TemplateDecision) -> RestoreMigrateStrategy:
        strategy = _strategy()
        with (
            patch.object(strategy, "_decide_template", return_value=decision),
            patch("fraisier.dbops.restore.find_latest_backup") as find,
            patch("fraisier.dbops.restore.validate_backup_age", return_value=True),
            patch("fraisier.dbops.operations.terminate_backends"),
            patch("fraisier.dbops.operations.drop_db", return_value=(0, "", "")),
            patch("fraisier.dbops.operations.create_db", return_value=(0, "", "")),
            patch(
                "fraisier.dbops.restore.restore_backup",
                return_value=RestoreResult(success=True),
            ),
            patch(
                "fraisier.strategies._restore.migrate_up",
                return_value=MigrationResult(success=True, steps_applied=1),
            ),
            patch.object(strategy, "_check_tviews_not_empty"),
            patch.object(strategy, "_record_actuation_of"),
        ):
            find.return_value = Path("/backup/db.dump")
            strategy.execute(CONFIG, migrations_dir=MDIR)
        return strategy

    def test_a_skipped_template_rolls_back_with_migrate_down(self):
        strategy = self._executed(_SKIP)

        with (
            patch(
                "fraisier.strategies._restore.migrate_down",
                return_value=MigrationResult(success=True, steps_applied=1),
            ) as down,
            patch("fraisier.dbops.templates.reset_from_template") as reset,
        ):
            result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=1)

        assert result.success
        assert down.call_args.kwargs["steps"] == 1
        reset.assert_not_called()

    def test_a_taken_template_rolls_back_onto_it(self):
        strategy = self._executed(_TAKE)

        with (
            patch("fraisier.strategies._restore.migrate_down") as down,
            patch("fraisier.dbops.templates.reset_from_template") as reset,
        ):
            reset.return_value = MagicMock(success=True)
            result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=1)

        assert result.success
        reset.assert_called_once()
        down.assert_not_called()

    def test_a_failed_down_is_a_failed_rollback(self):
        strategy = self._executed(_SKIP)

        with patch(
            "fraisier.strategies._restore.migrate_down",
            return_value=MigrationResult(
                success=False, steps_applied=0, errors=["down failed"]
            ),
        ):
            result = strategy.rollback(CONFIG, migrations_dir=MDIR, steps=1)

        assert result.success is False
        assert result.errors == ["down failed"]


def _deployer(tmp_path):
    from fraisier.deployers.api import APIDeployer

    deployer = APIDeployer(
        {
            "app_path": str(tmp_path),
            "database": {
                "strategy": "restore_migrate",
                "name": "staging_db",
                "admin_url": _ADMIN_URL,
                "restore": {"backup_dir": "/backup", "create_template": True},
            },
        }
    )
    deployer._previous_sha = "prev123"
    return deployer


class TestTheDeployerRollsBackWhatTheRunDid:
    def test_a_failure_after_migrate_records_the_count(self, tmp_path):
        deployer = _deployer(tmp_path)
        strategy = MagicMock()
        error = DatabaseError("Table count validation failed: 3 < 99")
        error.steps_applied = 2
        strategy.execute.side_effect = error

        with (
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(strategy, Path("c.yaml"), Path("m"), None),
            ),
            pytest.raises(DatabaseError),
        ):
            deployer._run_strategy()

        assert deployer._migrations_applied == 2

    def test_the_rollback_asks_the_strategy_that_ran(self, tmp_path):
        """A fresh strategy would not know whether step 8 copied."""
        deployer = _deployer(tmp_path)
        ran = MagicMock()
        ran.execute.side_effect = DatabaseError("boom")
        ran.execute.side_effect.steps_applied = 1
        ran.rollback.return_value = MagicMock(success=True, migrations_applied=1)
        fresh = MagicMock()
        resolved = iter(
            [
                (ran, Path("c.yaml"), Path("m"), None),
                (fresh, Path("c.yaml"), Path("m"), None),
            ]
        )

        with patch.object(
            deployer, "_resolve_strategy", side_effect=lambda: next(resolved)
        ):
            with pytest.raises(DatabaseError):
                deployer._run_strategy()
            deployer._rollback_database(None, "prev123")

        ran.rollback.assert_called_once()
        fresh.rollback.assert_not_called()

    def test_a_failed_down_reports_rollback_failed(self, tmp_path):
        deployer = _deployer(tmp_path)
        ran = MagicMock()
        ran.rollback.return_value = MagicMock(
            success=False, migrations_applied=0, errors=["down failed"]
        )
        deployer._executed_strategy = ran
        deployer._migrations_applied = 1

        with (
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(ran, Path("c.yaml"), Path("m"), None),
            ),
            patch.object(deployer, "_write_incident"),
        ):
            result = deployer._rollback_database(None, "prev123")

        assert result.status is DeploymentStatus.ROLLBACK_FAILED

    def test_a_down_that_raises_fails_the_deploy_as_rollback_failed(self, tmp_path):
        """confiture raises on a failing down SQL; that is never ROLLED_BACK."""
        from confiture.exceptions import MigrationError as ConfitureMigrationError

        deployer = _deployer(tmp_path)
        ran = MagicMock()
        ran.rollback.side_effect = ConfitureMigrationError(
            "Failed to rollback migration 002 (add_w)"
        )
        deployer._executed_strategy = ran
        deployer._migrations_applied = 1

        with (
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(ran, Path("c.yaml"), Path("m"), None),
            ),
            patch.object(deployer, "_git_rollback") as git_rollback,
        ):
            outcome = deployer._restore_previous_state()
        status, state, message = deployer._classify_failure(outcome, "boom")

        assert status is DeploymentStatus.ROLLBACK_FAILED
        assert state == "rollback_failed"
        assert "Failed to rollback migration 002" in message
        # Old code is never put back on a half-undone schema.
        git_rollback.assert_not_called()

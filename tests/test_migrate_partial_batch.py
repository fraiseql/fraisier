"""A partially-applied migration batch must be rolled back (#272).

When migration N applies cleanly and N+1 fails, confiture returns
``has_errors=True`` with a populated ``migrations_applied``. That count was
discarded at the raise, so ``APIDeployer._migrations_applied`` stayed 0 and
``_restore_previous_state`` took the "nothing applied, skip DB rollback" branch
— leaving the schema half-migrated with no incident.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.errors import MigrationError


class _FakeMigrateUpResult:
    """Stand-in for confiture's MigrateUpResult."""

    def __init__(self, applied: list[str], has_errors: bool, summary: str = ""):
        self.migrations_applied = applied
        self.has_errors = has_errors
        self.error_summary = summary


class _FakeMigrator:
    """Context-manager stand-in for confiture's Migrator."""

    def __init__(self, result=None, raises: Exception | None = None):
        self._result = result
        self._raises = raises
        self._conn = MagicMock()
        self.migration = MagicMock()
        self.migration.view_helpers = "off"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def up(self, **_kwargs):
        if self._raises is not None:
            raise self._raises
        return self._result


def _run_migrate_up(tmp_path: Path, migrator: _FakeMigrator):
    """Invoke dbops.migrate_up against a stubbed Migrator."""
    from fraisier.dbops import confiture as dbops

    env = MagicMock()
    env.migration.view_helpers = "off"

    with (
        patch.object(dbops, "_load_env", return_value=env),
        patch.object(dbops, "Migrator") as mock_migrator_cls,
        patch.object(dbops, "_register_migration_hooks"),
    ):
        mock_migrator_cls.from_config.return_value = migrator
        return dbops.migrate_up(
            tmp_path / "confiture.yaml",
            migrations_dir=tmp_path / "migrations",
            database_url="postgresql:///x",
        )


class TestMigrateUpCarriesAppliedCount:
    """The count confiture reports must survive the raise."""

    def test_partial_batch_error_carries_steps_applied(self, tmp_path):
        """2 of 3 applied → the exception knows 2 were applied."""
        migrator = _FakeMigrator(
            _FakeMigrateUpResult(
                applied=["0001", "0002"],
                has_errors=True,
                summary="Failed to apply migration 0003",
            )
        )

        with pytest.raises(MigrationError) as exc_info:
            _run_migrate_up(tmp_path, migrator)

        assert exc_info.value.steps_applied == 2

    def test_steps_applied_reaches_the_context_dict(self, tmp_path):
        """The count is serialised with the error, not just held as an attribute.

        `context` is what survives into status.error_message and the
        authenticated /api/status/<fraise>/details payload.
        """
        migrator = _FakeMigrator(
            _FakeMigrateUpResult(applied=["0001"], has_errors=True, summary="boom")
        )

        with pytest.raises(MigrationError) as exc_info:
            _run_migrate_up(tmp_path, migrator)

        assert exc_info.value.context.get("steps_applied") == 1

    def test_nothing_applied_reports_zero(self, tmp_path):
        """A batch that fails on its first migration reports 0, not None."""
        migrator = _FakeMigrator(
            _FakeMigrateUpResult(applied=[], has_errors=True, summary="boom")
        )

        with pytest.raises(MigrationError) as exc_info:
            _run_migrate_up(tmp_path, migrator)

        assert exc_info.value.steps_applied == 0

    def test_success_path_unchanged(self, tmp_path):
        """A clean batch still returns a MigrationResult with the count."""
        migrator = _FakeMigrator(
            _FakeMigrateUpResult(applied=["0001", "0002"], has_errors=False)
        )

        result = _run_migrate_up(tmp_path, migrator)

        assert result.success is True
        assert result.steps_applied == 2


class TestMigrationErrorStepsApplied:
    """`steps_applied` must not be confused with the pre-existing `step`."""

    def test_defaults_to_none(self):
        """Omitted → None, meaning 'unknown', which callers treat as skip."""
        error = MigrationError("boom")

        assert error.steps_applied is None

    def test_is_distinct_from_step(self):
        """`step` is a 1-indexed position; `steps_applied` is a count."""
        error = MigrationError("boom", step=4, steps_applied=3)

        assert error.step == 4
        assert error.steps_applied == 3


def _deployer(tmp_path):
    """An APIDeployer with a database config and a known previous SHA."""
    from fraisier.deployers.api import APIDeployer

    deployer = APIDeployer(
        {
            "app_path": str(tmp_path),
            "database": {"strategy": "migrate", "name": "testdb"},
        }
    )
    deployer._previous_sha = "prev123"
    return deployer


class TestDeployerRecordsCountOnFailure:
    """_run_strategy must capture the count before the exception escapes."""

    def test_migrations_applied_set_from_exception(self, tmp_path):
        """A raising strategy still leaves the true count on the deployer."""
        deployer = _deployer(tmp_path)
        strategy = MagicMock()
        strategy.execute.side_effect = MigrationError("boom", steps_applied=2)

        with (
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(strategy, Path("c.yaml"), Path("m"), None),
            ),
            patch.object(
                deployer,
                "_resolve_paths_against_app",
                side_effect=lambda a, b: (a, b),
            ),
            pytest.raises(MigrationError),
        ):
            deployer._run_strategy()

        assert deployer._migrations_applied == 2

    def test_unknown_count_leaves_zero(self, tmp_path):
        """steps_applied=None means 'unknown' → stay at 0, skip DB rollback.

        Conservative on purpose: rolling back an unknown number of migrations
        is worse than leaving the schema for an operator.
        """
        deployer = _deployer(tmp_path)
        strategy = MagicMock()
        strategy.execute.side_effect = MigrationError("boom")

        with (
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(strategy, Path("c.yaml"), Path("m"), None),
            ),
            patch.object(
                deployer,
                "_resolve_paths_against_app",
                side_effect=lambda a, b: (a, b),
            ),
            pytest.raises(MigrationError),
        ):
            deployer._run_strategy()

        assert deployer._migrations_applied == 0

    def test_original_exception_propagates_unchanged(self, tmp_path):
        """Recording the count must not swallow or reshape the error."""
        deployer = _deployer(tmp_path)
        original = MigrationError("the real cause", steps_applied=1)
        strategy = MagicMock()
        strategy.execute.side_effect = original

        with (
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(strategy, Path("c.yaml"), Path("m"), None),
            ),
            patch.object(
                deployer,
                "_resolve_paths_against_app",
                side_effect=lambda a, b: (a, b),
            ),
            pytest.raises(MigrationError) as exc_info,
        ):
            deployer._run_strategy()

        assert exc_info.value is original


class TestPartialBatchTriggersDbRollback:
    """The end-to-end guarantee #272 asks for."""

    def test_restore_rolls_back_the_applied_migrations(self, tmp_path):
        """2 applied then a failure → migrate down 2."""
        deployer = _deployer(tmp_path)
        deployer._migrations_applied = 2

        rollback_result = MagicMock()
        rollback_result.success = True
        rollback_result.migrations_applied = 2
        rollback_result.errors = []

        strategy = MagicMock()
        strategy.rollback.return_value = rollback_result

        with (
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(strategy, Path("c.yaml"), Path("m"), None),
            ),
            patch.object(
                deployer,
                "_resolve_paths_against_app",
                side_effect=lambda a, b: (a, b),
            ),
            patch.object(deployer, "_git_rollback"),
            patch.object(deployer, "_restart_service"),
        ):
            deployer._restore_previous_state()

        strategy.rollback.assert_called_once()
        assert strategy.rollback.call_args.kwargs["steps"] == 2

    def test_zero_applied_still_skips_db_rollback(self, tmp_path):
        """The pre-existing skip branch is still correct when truly zero."""
        deployer = _deployer(tmp_path)
        deployer._migrations_applied = 0

        strategy = MagicMock()

        with (
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(strategy, Path("c.yaml"), Path("m"), None),
            ),
            patch.object(deployer, "_git_rollback"),
            patch.object(deployer, "_restart_service"),
        ):
            deployer._restore_previous_state()

        strategy.rollback.assert_not_called()


class TestIncidentOnPartialRollbackFailure:
    """When the rollback itself only partly succeeds, say so precisely."""

    def test_incident_records_rolled_back_and_remaining(self, tmp_path):
        """2 applied, rollback undoes 1 → incident names 1 rolled back, 1 left."""
        deployer = _deployer(tmp_path)
        deployer._migrations_applied = 2

        rollback_result = MagicMock()
        rollback_result.success = False
        rollback_result.migrations_applied = 1
        rollback_result.errors = ["down migration 0002 failed"]

        strategy = MagicMock()
        strategy.rollback.return_value = rollback_result

        with (
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(strategy, Path("c.yaml"), Path("m"), None),
            ),
            patch.object(
                deployer,
                "_resolve_paths_against_app",
                side_effect=lambda a, b: (a, b),
            ),
            patch.object(deployer, "_write_incident") as mock_incident,
        ):
            deployer._rollback_database(None, "prev123")

        mock_incident.assert_called_once()
        message = mock_incident.call_args.args[0]
        assert "Rolled back 1 of 2" in message
        assert "1 still applied" in message
        assert "Do NOT restart the service" in message


class TestRestoreMigrateBlastRadius:
    """restore_migrate now reaches its rollback on a partial batch too.

    `dbops.migrate_up` is shared by MigrateStrategy, ConfitureMigrateStrategy
    and RestoreMigrateStrategy, and `_run_strategy` is strategy-agnostic — so
    fixing the count changes behaviour for all three. RestoreMigrateStrategy's
    rollback ignores `steps` and performs a template reset / drop+create, which
    previously never fired on this path.
    """

    def test_restore_migrate_rollback_is_invoked(self, tmp_path):
        """A partial batch under restore_migrate triggers its rollback."""
        from fraisier.deployers.api import APIDeployer

        deployer = APIDeployer(
            {
                "app_path": str(tmp_path),
                "database": {"strategy": "restore_migrate", "name": "testdb"},
            }
        )
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 1

        rollback_result = MagicMock()
        rollback_result.success = True
        rollback_result.migrations_applied = 1
        rollback_result.errors = []

        strategy = MagicMock()
        strategy.rollback.return_value = rollback_result

        with (
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(strategy, Path("c.yaml"), Path("m"), None),
            ),
            patch.object(
                deployer,
                "_resolve_paths_against_app",
                side_effect=lambda a, b: (a, b),
            ),
            patch.object(deployer, "_git_rollback"),
            patch.object(deployer, "_restart_service"),
        ):
            deployer._restore_previous_state()

        strategy.rollback.assert_called_once()


class TestKnownGapsArePinned:
    """Things this fix deliberately does NOT change (see #293)."""

    def test_first_deploy_without_previous_sha_still_skips(self, tmp_path):
        """No previous SHA → _restore_previous_state is a no-op.

        A first deploy with a partial batch still leaves the schema dirty.
        Out of scope here; pinned so the limitation is visible.
        """
        deployer = _deployer(tmp_path)
        deployer._previous_sha = None
        deployer._migrations_applied = 2

        strategy = MagicMock()

        with patch.object(
            deployer,
            "_resolve_strategy",
            return_value=(strategy, Path("c.yaml"), Path("m"), None),
        ):
            deployer._restore_previous_state()

        strategy.rollback.assert_not_called()

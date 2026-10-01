"""A pgBackRest refresh is covered by the same deployment lock, and fails closed (#424).

The lock is taken by the *callers* — the CLI, the webhook and the daemon — not by
the strategy, so the pgBackRest source inherits it only because it runs inside
``RestoreMigrateStrategy.execute``. These pin that with the real file lock rather
than a mock, because the claim is about exclusion:

* a held lock stops a refresh before it so much as asks the helper for ``info``;
* while a refresh is stopping and rewriting the cluster, no deploy of the same
  fraise can start;
* and a refresh that **fails closed** is not undone by whatever handles the
  failure: neither the CLI nor a failed deploy restarts the service against a
  half-restored cluster.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.deployers.api import APIDeployer
from fraisier.errors import DatabaseError, DeploymentLockError, RestoreFailedClosed
from fraisier.locking import deployment_lock
from fraisier.strategies import RestoreMigrateStrategy
from fraisier.strategies._restore_sources import PgBackRestSource

PGBACKREST_DB = {
    "name": "app",
    "strategy": "restore_migrate",
    "admin_url": "postgresql://postgres@localhost:5433/postgres",
    "confiture_config": "confiture.yaml",
    "restore": {
        "source": "pgbackrest",
        "pgbackrest": {"stanza": "main", "repo": 1, "cluster": "18/staging"},
    },
}


@pytest.fixture
def cfg():
    config = MagicMock()
    config.project_name = "proj"
    config.get_fraise.return_value = {"type": "api"}
    config.get_fraise_environment.return_value = {
        "type": "api",
        "app_path": "/var/www/api",
        "systemd_service": "api.staging.service",
        "database": PGBACKREST_DB,
    }
    config._config = {"backup": {}}
    with patch("fraisier.cli.main.get_config", return_value=config):
        yield config


@pytest.fixture
def helper_calls():
    """A ``HelperClient`` that records what it was asked and answers like a quiet helper."""
    calls: list[str] = []

    class Recorder:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def info(self) -> str:
            calls.append("info")
            raise AssertionError(
                "the helper must not be asked anything without the lock"
            )

    with patch("fraisier.dbops.pgbackrest.HelperClient", Recorder):
        yield calls


def run_restore(*extra: str):
    return CliRunner().invoke(main, ["db", "restore", "api", "staging", *extra])


class TestTheCli:
    def test_a_held_lock_stops_the_refresh_before_the_helper_is_asked_anything(
        self, cfg, helper_calls
    ) -> None:
        with deployment_lock("api"):  # a deploy of the same fraise is running
            result = run_restore()

        assert result.exit_code != 0
        assert helper_calls == []

    def test_skip_if_locked_is_a_quiet_zero_and_still_asks_nothing(
        self, cfg, helper_calls
    ) -> None:
        with deployment_lock("api"):
            result = run_restore("--skip-if-locked")

        assert result.exit_code == 0, result.output
        assert helper_calls == []

    def test_the_refresh_runs_inside_the_strategy_under_the_lock(self, cfg) -> None:
        """The pgBackRest source is reached only through ``RestoreMigrateStrategy.execute``."""
        seen: dict[str, Any] = {}

        def execute(self: RestoreMigrateStrategy, *_a: Any, **_k: Any):
            seen["source"] = self._source
            try:
                with deployment_lock("api"):
                    seen["lock_was_held"] = False
            except DeploymentLockError:
                seen["lock_was_held"] = True
            return MagicMock(
                success=True,
                migrations_applied=0,
                total_duration_seconds=0.0,
                restore_duration_seconds=0.0,
                migration_duration_seconds=0.0,
                schema_floor=None,
                unchecked_schemas=(),
                actuation=None,
            )

        with (
            patch.object(RestoreMigrateStrategy, "execute", execute),
            patch("fraisier.post_migrate.run_configured_post_migrate"),
            patch("fraisier.service_managers.get_service_manager"),
        ):
            result = run_restore()

        assert result.exit_code == 0, result.output
        assert isinstance(seen["source"], PgBackRestSource)
        assert seen["lock_was_held"], "the refresh ran without holding the lock"

    def test_while_the_cluster_is_being_rewritten_no_deploy_can_start(
        self, cfg
    ) -> None:
        """Probed from inside the helper's own ``restore`` call, where it matters."""
        outcome: dict[str, bool] = {}

        class Helper:
            def __init__(self, *_a: Any, **_k: Any) -> None:
                pass

            def info(self) -> str:
                return (
                    __import__("pathlib")
                    .Path(__file__)
                    .parent.joinpath("fixtures/pgbackrest/info-full-and-incrs.json")
                    .read_text()
                )

            def stop(self) -> dict:
                return {"ok": True}

            def restore(self, label: str) -> dict:
                try:
                    with deployment_lock("api"):
                        outcome["deploy_could_start"] = True
                except DeploymentLockError:
                    outcome["deploy_could_start"] = False
                raise RuntimeError("stop here; the lock was all this was probing")

        with (
            patch("fraisier.dbops.pgbackrest.HelperClient", Helper),
            patch("fraisier.service_managers.get_service_manager"),
            patch("fraisier.strategies._restore_sources.datetime") as clock,
        ):
            clock.now.return_value = __import__("datetime").datetime(
                2026, 10, 1, 17, 0, tzinfo=__import__("datetime").UTC
            )
            run_restore()

        assert outcome == {"deploy_could_start": False}


class TestTheWebhook:
    def test_the_webhook_deploy_runs_while_holding_the_lock(self) -> None:
        """A webhook deploy of a pgBackRest fraise reaches the strategy the same way."""
        from fraisier.webhook import execute_deployment

        outcome: dict[str, bool] = {}

        async def probe(*_a: Any, **_k: Any) -> None:
            try:
                with deployment_lock("api"):
                    outcome["lock_was_held"] = False
            except DeploymentLockError:
                outcome["lock_was_held"] = True

        with (
            patch("fraisier.webhook.read_status", return_value=None),
            patch("fraisier.database.get_db"),
            patch("fraisier.webhook._run_deployment", probe),
        ):
            asyncio.run(
                execute_deployment(
                    fraise_name="api",
                    environment="staging",
                    fraise_config={"type": "api", "database": PGBACKREST_DB},
                    git_commit="sha",
                )
            )

        assert outcome == {"lock_was_held": True}

    def test_the_deployer_builds_the_pgbackrest_source_with_the_helper_socket(
        self,
    ) -> None:
        deployer = APIDeployer(
            {
                "fraise_name": "api",
                "environment": "staging",
                "app_path": "/var/www/api",
                "systemd_service": "api.staging.service",
                "database": PGBACKREST_DB,
            },
            config_object=MagicMock(project_name="proj"),
        )

        strategy, *_ = deployer._resolve_strategy()

        assert isinstance(strategy, RestoreMigrateStrategy)
        assert isinstance(strategy._source, PgBackRestSource)
        assert strategy._config.pgbackrest_socket == (
            "/run/fraisier/pgbackrest-proj-api-staging.sock"
        )


class TestFailingClosedIsNotUndone:
    def test_the_cli_leaves_the_service_stopped(self, cfg) -> None:
        manager = MagicMock()
        with (
            patch.object(
                RestoreMigrateStrategy,
                "execute",
                side_effect=RestoreFailedClosed("archive_mode is 'on'"),
            ),
            patch(
                "fraisier.service_managers.get_service_manager", return_value=manager
            ),
        ):
            result = run_restore()

        assert result.exit_code == 1
        assert "archive_mode" in result.output
        assert "left stopped" in result.output
        manager.restart.assert_not_called()

    def test_an_ordinary_failure_still_restarts_the_service(self, cfg) -> None:
        """Unchanged: the dump path restarts the app after a failed restore."""
        manager = MagicMock()
        with (
            patch.object(
                RestoreMigrateStrategy, "execute", side_effect=DatabaseError("nope")
            ),
            patch(
                "fraisier.service_managers.get_service_manager", return_value=manager
            ),
        ):
            result = run_restore()

        assert result.exit_code == 1
        manager.restart.assert_called_once()

    def deployer(self) -> APIDeployer:
        deployer = APIDeployer(
            {
                "app_path": "/var/www/api",
                "systemd_service": "api.staging.service",
                "database": PGBACKREST_DB,
            }
        )
        deployer._previous_sha = "oldsha"
        return deployer

    def test_a_failed_deploy_rolls_the_tree_back_but_does_not_restart_the_service(
        self,
    ) -> None:
        deployer = self.deployer()
        with (
            patch.object(deployer, "_git_rollback") as rollback,
            patch.object(deployer, "_restore_synced_config"),
            patch.object(deployer, "_restart_service") as restart,
        ):
            outcome = deployer._restore_previous_state(keep_service_stopped=True)

        rollback.assert_called_once()
        restart.assert_not_called()
        assert outcome.git_reverted is True
        assert outcome.service_restarted is False

    def test_the_default_still_restarts_it(self) -> None:
        deployer = self.deployer()
        with (
            patch.object(deployer, "_git_rollback"),
            patch.object(deployer, "_restore_synced_config"),
            patch.object(deployer, "_restart_service") as restart,
        ):
            outcome = deployer._restore_previous_state()

        restart.assert_called_once()
        assert outcome.service_restarted is True

    def test_execute_hands_the_flag_to_the_rollback(self) -> None:
        deployer = self.deployer()
        stubbed = (
            "_install_dependencies",
            "_check_service_file_staleness",
            "_validate_wrapper_scripts",
            "_validate_sandbox_writes",
            "_sync_config_if_needed",
            "_snapshot_version_json",
            "_generate_version_json",
            "_restore_version_json",
            "_write_status",
            "_complete_db_record",
            "_notify",
        )
        with contextlib.ExitStack() as stack:
            for name in stubbed:
                stack.enter_context(patch.object(deployer, name))
            stack.enter_context(
                patch.object(deployer, "_git_pull", return_value=("old", "new"))
            )
            stack.enter_context(
                patch.object(deployer, "_start_db_record", return_value=None)
            )
            stack.enter_context(
                patch.object(
                    deployer,
                    "_run_database_migrations",
                    side_effect=RestoreFailedClosed("x"),
                )
            )
            restore = stack.enter_context(
                patch.object(
                    deployer,
                    "_restore_previous_state",
                    return_value=MagicMock(
                        db_rollback_attempted=False,
                        db_rollback_succeeded=False,
                        git_reverted=True,
                        service_restarted=False,
                        error_message=None,
                    ),
                )
            )
            deployer.execute()

        assert restore.call_args.kwargs == {"keep_service_stopped": True}

"""Tests for APIDeployer rollback on health check failure.

Verifies that failed health checks trigger automatic rollback to
the previous SHA, and that the status file is updated at each
state transition.
"""

from unittest.mock import MagicMock, patch

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentResult, DeploymentStatus
from fraisier.errors import DeploymentError
from fraisier.health_check import HealthCheckResult
from fraisier.status import read_status
from fraisier.strategies import StrategyResult


def _make_deployer(tmp_path, **overrides):
    config = {
        "fraise_name": "myapi",
        "environment": "production",
        "app_path": "/srv/myapi",
        "clone_url": "git@github.com:org/myapi.git",
        "branch": "main",
        "systemd_service": "myapi.service",
        "health_check": {"url": "http://localhost:8000/health", "timeout": 5},
        "repos_base": str(tmp_path / "repos"),
        "status_dir": str(tmp_path / "status"),
    }
    config.update(overrides)
    return APIDeployer(config)


class TestAutoRollbackOnHealthFailure:
    """When health check fails after deploy, auto-rollback to previous SHA."""

    def test_execute_rolls_back_on_health_failure(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        fail_result = HealthCheckResult(
            success=False,
            check_type="http",
            duration=5.0,
            message="down",
        )
        ok_result = HealthCheckResult(
            success=True,
            check_type="http",
            duration=0.1,
        )
        health_calls = [fail_result, ok_result]

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("oldsha111", "newsha222"),
            ),
            patch("subprocess.run"),
            patch("fraisier.deployers.api.HealthCheckManager") as MockMgr,
            patch("fraisier.deployers.api.HTTPHealthChecker"),
        ):
            MockMgr.return_value.check_with_retries.side_effect = health_calls
            result = deployer.execute()

        assert result.status == DeploymentStatus.ROLLED_BACK
        assert result.old_version == "oldsha11"
        assert result.new_version == "oldsha11"

    def test_rollback_checks_out_previous_sha(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        deployer._previous_sha = "prev_sha_abc123"

        with (
            patch("subprocess.run") as mock_run,
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            result = deployer.rollback()

        assert result.success is True
        assert result.status == DeploymentStatus.ROLLED_BACK
        # Verify checkout was called with previous SHA
        checkout_calls = [c for c in mock_run.call_args_list if "checkout" in str(c)]
        assert len(checkout_calls) > 0
        assert "prev_sha_abc123" in str(checkout_calls[0])

    def test_rollback_restarts_service(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        deployer._previous_sha = "prev123"

        with (
            patch("subprocess.run"),
            patch.object(deployer, "_restart_service") as mock_restart,
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            deployer.rollback()

        mock_restart.assert_called_once()

    def test_rollback_fails_without_previous_sha(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        # No _previous_sha set

        result = deployer.rollback()

        assert result.success is False
        assert "No previous SHA" in result.error_message

    def test_execute_returns_rolled_back_not_failed(self, tmp_path):
        """Auto-rollback should return ROLLED_BACK, not FAILED."""
        deployer = _make_deployer(tmp_path)
        fail = HealthCheckResult(
            success=False,
            check_type="http",
            duration=1.0,
        )
        ok = HealthCheckResult(success=True, check_type="http", duration=0.1)

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch("subprocess.run"),
            patch("fraisier.deployers.api.HealthCheckManager") as MockMgr,
            patch("fraisier.deployers.api.HTTPHealthChecker"),
        ):
            MockMgr.return_value.check_with_retries.side_effect = [fail, ok]
            result = deployer.execute()

        assert result.status == DeploymentStatus.ROLLED_BACK
        assert result.success is False


class TestRollbackAbortsOnMigrationFailure:
    """If migration rollback fails, do NOT proceed to git checkout."""

    def test_rollback_aborts_on_migration_down_failure(self, tmp_path):
        from unittest.mock import MagicMock

        deployer = _make_deployer(
            tmp_path,
            database={"strategy": "migrate", "confiture_config": "confiture.yaml"},
        )
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 3

        mock_strategy = MagicMock()
        mock_strategy.rollback.return_value = StrategyResult(
            success=False,
            errors=["down migration 003 failed: file not found"],
        )

        with (
            patch("fraisier.strategies.get_strategy", return_value=mock_strategy),
            patch("subprocess.run") as mock_subprocess,
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            result = deployer.rollback()

        assert not result.success
        assert result.status == DeploymentStatus.FAILED
        assert "manual intervention required" in result.error_message.lower()
        # Git checkout must NOT have been called
        git_calls = [c for c in mock_subprocess.call_args_list if "checkout" in str(c)]
        assert len(git_calls) == 0

    def test_rollback_aborts_sets_clear_error_message(self, tmp_path):
        from unittest.mock import MagicMock

        deployer = _make_deployer(
            tmp_path,
            database={"strategy": "migrate", "confiture_config": "confiture.yaml"},
        )
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 2

        mock_strategy = MagicMock()
        mock_strategy.rollback.return_value = StrategyResult(
            success=False,
            errors=["Cannot apply down: 002_add_orders.down.sql not found"],
        )

        with (
            patch("fraisier.strategies.get_strategy", return_value=mock_strategy),
            patch("subprocess.run"),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            result = deployer.rollback()

        assert "002_add_orders" in result.error_message
        assert "manual intervention required" in result.error_message.lower()


class TestStatusFileUpdates:
    """Status file is written at each state transition."""

    def test_status_file_written_deploying_on_start(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        status_dir = tmp_path / "status"

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch("subprocess.run"),
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            deployer.execute()

        status = read_status("myapi", status_dir=status_dir)
        assert status is not None

    def test_status_file_success_after_deploy(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        status_dir = tmp_path / "status"

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch("subprocess.run"),
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            deployer.execute()

        status = read_status("myapi", status_dir=status_dir)
        assert status is not None
        assert status.state == "success"
        assert status.commit_sha == "new"

    def test_status_file_failed_on_error(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        status_dir = tmp_path / "status"

        with (
            patch(
                "fraisier.deployers.mixins.clone_bare_repo",
                side_effect=RuntimeError("clone failed"),
            ),
        ):
            deployer.execute()

        status = read_status("myapi", status_dir=status_dir)
        assert status is not None
        assert status.state == "failed"
        assert "clone failed" in status.error_message

    def test_status_file_rolled_back_after_auto_rollback(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        status_dir = tmp_path / "status"
        fail = HealthCheckResult(
            success=False,
            check_type="http",
            duration=1.0,
        )
        ok = HealthCheckResult(success=True, check_type="http", duration=0.1)

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old_sha", "new_sha"),
            ),
            patch("subprocess.run"),
            patch("fraisier.deployers.api.HealthCheckManager") as MockMgr,
            patch("fraisier.deployers.api.HTTPHealthChecker"),
        ):
            MockMgr.return_value.check_with_retries.side_effect = [fail, ok]
            deployer.execute()

        status = read_status("myapi", status_dir=status_dir)
        assert status is not None
        assert status.state == "rolled_back"

    def test_rollback_method_updates_status_file(self, tmp_path):
        deployer = _make_deployer(tmp_path)
        deployer._previous_sha = "prev123"
        status_dir = tmp_path / "status"

        with (
            patch("subprocess.run"),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            deployer.rollback()

        status = read_status("myapi", status_dir=status_dir)
        assert status is not None
        assert status.state == "rolled_back"


class TestMigrationFailureTriggersGitRollback:
    """When migration fails after git checkout, git must be rolled back."""

    def test_migration_failure_triggers_git_rollback(self, tmp_path):
        """If _run_strategy() raises, git should be rolled back to old SHA."""
        deployer = _make_deployer(
            tmp_path,
            database={"strategy": "migrate", "confiture_config": "confiture.yaml"},
        )

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("oldsha111111", "newsha222222"),
            ),
            patch.object(
                deployer,
                "_run_strategy",
                side_effect=DeploymentError("Migration 003 failed"),
            ),
            patch.object(deployer, "_git_rollback") as mock_git_rollback,
            patch.object(deployer, "_restart_service") as mock_restart,
        ):
            result = deployer.execute()

        assert not result.success
        mock_git_rollback.assert_called_once_with("oldsha111111")
        mock_restart.assert_called_once()

    def test_migration_failure_without_previous_sha_skips_rollback(self, tmp_path):
        """If no previous SHA (first deploy), don't attempt git rollback."""
        deployer = _make_deployer(
            tmp_path,
            database={"strategy": "migrate", "confiture_config": "confiture.yaml"},
        )

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=(None, "newsha222222"),
            ),
            patch.object(
                deployer,
                "_run_strategy",
                side_effect=DeploymentError("Migration failed"),
            ),
            patch.object(deployer, "_git_rollback") as mock_git_rollback,
        ):
            result = deployer.execute()

        assert not result.success
        mock_git_rollback.assert_not_called()

    def test_migration_failure_result_preserves_old_version(self, tmp_path):
        """Result should contain old_version for operator context."""
        deployer = _make_deployer(
            tmp_path,
            database={"strategy": "migrate", "confiture_config": "confiture.yaml"},
        )

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("oldsha111111", "newsha222222"),
            ),
            patch.object(
                deployer,
                "_run_strategy",
                side_effect=DeploymentError("Migration failed"),
            ),
            patch.object(deployer, "_git_rollback"),
            patch.object(deployer, "_restart_service"),
        ):
            result = deployer.execute()

        assert result.old_version == "oldsha11"
        assert "Migration failed" in result.error_message


class TestRestorePreviousStateRollsMigrationsBack:
    """_restore_previous_state must roll back DB migrations when they were applied."""

    def test_restore_rolls_back_migrations_when_applied(self, tmp_path):
        """If migrations were applied, _restore_previous_state must call
        _rollback_database before git rollback."""
        deployer = _make_deployer(
            tmp_path,
            database={"strategy": "migrate", "confiture_config": "confiture.yaml"},
        )
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 2

        mock_strategy = MagicMock()
        mock_strategy.rollback.return_value = StrategyResult(
            success=True,
            migrations_applied=2,
        )

        with (
            patch("fraisier.strategies.get_strategy", return_value=mock_strategy),
            patch.object(deployer, "_git_rollback") as mock_git_rollback,
            patch.object(deployer, "_restart_service"),
        ):
            deployer._restore_previous_state()

        mock_strategy.rollback.assert_called_once()
        mock_git_rollback.assert_called_once_with("prev123")

    def test_restore_skips_db_rollback_when_no_migrations(self, tmp_path):
        """If no migrations were applied, skip database rollback."""
        deployer = _make_deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 0

        with (
            patch.object(deployer, "_git_rollback") as mock_git_rollback,
            patch.object(deployer, "_restart_service"),
        ):
            deployer._restore_previous_state()

        mock_git_rollback.assert_called_once_with("prev123")

    def test_restore_order_db_before_git(self, tmp_path):
        """Database rollback must happen before git rollback."""
        deployer = _make_deployer(
            tmp_path,
            database={"strategy": "migrate", "confiture_config": "confiture.yaml"},
        )
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 1

        call_order = []

        def fake_db_rollback(current_version, target):
            call_order.append("db_rollback")
            return DeploymentResult(
                success=True,
                status=DeploymentStatus.ROLLED_BACK,
                duration_seconds=0,
                details={"migrations_rolled_back": 1},
            )

        with (
            patch.object(
                deployer, "_rollback_database", side_effect=fake_db_rollback
            ),
            patch.object(
                deployer,
                "_git_rollback",
                side_effect=lambda _sha: call_order.append("git_rollback"),
            ),
            patch.object(
                deployer,
                "_restart_service",
                side_effect=lambda: call_order.append("restart"),
            ),
        ):
            deployer._restore_previous_state()

        assert call_order == ["db_rollback", "git_rollback", "restart"]


class TestDoubleFailureSendsCriticalNotification:
    """If rollback itself fails, notification must reach the operator."""

    def test_double_failure_sends_critical_notification(self, tmp_path):
        """When health check fails AND rollback fails, notify with ROLLBACK_FAILED."""
        deployer = _make_deployer(tmp_path)
        deployer._dispatcher = MagicMock()
        deployer._dispatcher.is_configured = True

        fail_health = HealthCheckResult(
            success=False, check_type="http", duration=5.0, message="down"
        )

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("oldsha111111", "newsha222222"),
            ),
            patch("subprocess.run"),
            patch("fraisier.deployers.api.HealthCheckManager") as MockMgr,
            patch("fraisier.deployers.api.HTTPHealthChecker"),
            patch.object(
                deployer,
                "rollback",
                return_value=DeploymentResult(
                    success=False,
                    status=DeploymentStatus.FAILED,
                    error_message="migrate down failed: file not found",
                ),
            ),
        ):
            MockMgr.return_value.check_with_retries.return_value = fail_health
            result = deployer.execute()

        assert result.status == DeploymentStatus.ROLLBACK_FAILED
        deployer._dispatcher.notify.assert_called_once()
        event = deployer._dispatcher.notify.call_args[0][0]
        assert event.event_type == "rollback_failed"

    def test_double_failure_includes_both_errors(self, tmp_path):
        """ROLLBACK_FAILED result must include both original and rollback errors."""
        deployer = _make_deployer(tmp_path)

        fail_health = HealthCheckResult(
            success=False, check_type="http", duration=5.0, message="down"
        )

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("oldsha111111", "newsha222222"),
            ),
            patch("subprocess.run"),
            patch("fraisier.deployers.api.HealthCheckManager") as MockMgr,
            patch("fraisier.deployers.api.HTTPHealthChecker"),
            patch.object(
                deployer,
                "rollback",
                return_value=DeploymentResult(
                    success=False,
                    status=DeploymentStatus.FAILED,
                    error_message="migrate down failed",
                ),
            ),
        ):
            MockMgr.return_value.check_with_retries.return_value = fail_health
            result = deployer.execute()

        assert "Health check failed" in result.error_message
        assert "migrate down failed" in result.error_message

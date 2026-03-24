"""Tests for APIDeployer rollback on health check failure.

Verifies that failed health checks trigger automatic rollback to
the previous SHA, and that the status file is updated at each
state transition.
"""

from unittest.mock import patch

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentStatus
from fraisier.health_check import HealthCheckResult
from fraisier.status import read_status


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

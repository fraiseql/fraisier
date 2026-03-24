"""Tests for APIDeployer service restart + health check.

Verifies that after git checkout, the deployer restarts systemd
and polls health with exponential backoff via HealthCheckManager.
"""

from unittest.mock import patch

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentStatus
from fraisier.health_check import HealthCheckResult


def _make_deployer(**overrides):
    config = {
        "fraise_name": "myapi",
        "environment": "production",
        "app_path": "/srv/myapi",
        "clone_url": "git@github.com:org/myapi.git",
        "branch": "main",
        "systemd_service": "myapi.service",
        "health_check": {"url": "http://localhost:8000/health", "timeout": 5},
        "repos_base": "/tmp/repos",
    }
    config.update(overrides)
    return APIDeployer(config)


class TestExecuteCallOrder:
    """Verify execute() calls steps in the right order."""

    def test_restart_happens_after_git_checkout(self):
        deployer = _make_deployer()
        call_order = []

        with (
            patch(
                "fraisier.deployers.mixins.clone_bare_repo",
            ),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old123", "new456"),
            ) as mock_fc,
            patch.object(
                deployer,
                "_restart_service",
                side_effect=lambda: call_order.append("restart"),
            ),
            patch.object(
                deployer,
                "_wait_for_health",
                side_effect=lambda: (
                    call_order.append("health"),
                    True,
                )[-1],
            ),
        ):
            mock_fc.side_effect = lambda *_a, **_kw: (
                call_order.append("git"),
                ("old123", "new456"),
            )[-1]
            result = deployer.execute()

        assert result.success is True
        assert call_order == ["git", "restart", "health"]

    def test_restart_not_called_when_no_service(self):
        deployer = _make_deployer(systemd_service=None)

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch.object(deployer, "_restart_service") as mock_restart,
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            deployer.execute()

        mock_restart.assert_not_called()


class TestHealthCheckWithBackoff:
    """Test _wait_for_health uses exponential backoff."""

    def test_returns_true_on_first_success(self):
        deployer = _make_deployer()
        ok_result = HealthCheckResult(success=True, check_type="http", duration=0.1)

        with patch("fraisier.deployers.api.HealthCheckManager") as MockMgr:
            MockMgr.return_value.check_with_retries.return_value = ok_result
            assert deployer._wait_for_health() is True

    def test_returns_false_after_all_retries_exhausted(self):
        deployer = _make_deployer()
        fail_result = HealthCheckResult(
            success=False,
            check_type="http",
            duration=0.1,
            message="Connection refused",
        )

        with patch("fraisier.deployers.api.HealthCheckManager") as MockMgr:
            MockMgr.return_value.check_with_retries.return_value = fail_result
            assert deployer._wait_for_health() is False

    def test_passes_backoff_params_to_manager(self):
        deployer = _make_deployer()
        ok_result = HealthCheckResult(success=True, check_type="http", duration=0.1)

        with patch("fraisier.deployers.api.HealthCheckManager") as MockMgr:
            MockMgr.return_value.check_with_retries.return_value = ok_result
            deployer._wait_for_health()

            call_kwargs = MockMgr.return_value.check_with_retries.call_args
            # Should pass exponential backoff parameters
            assert call_kwargs.kwargs.get("backoff_factor", 0) > 1

    def test_uses_http_health_checker(self):
        deployer = _make_deployer()
        ok_result = HealthCheckResult(success=True, check_type="http", duration=0.1)

        with (
            patch("fraisier.deployers.api.HealthCheckManager") as MockMgr,
            patch("fraisier.deployers.api.HTTPHealthChecker") as MockHTTP,
        ):
            MockMgr.return_value.check_with_retries.return_value = ok_result
            deployer._wait_for_health()

            MockHTTP.assert_called_once_with("http://localhost:8000/health")


class TestHealthCheckFailurePath:
    """Test that health check failure produces correct error in execute()."""

    def test_execute_rolls_back_on_health_check_error(self):
        deployer = _make_deployer()
        fail_result = HealthCheckResult(
            success=False,
            check_type="http",
            duration=5.0,
            message="Timeout",
        )
        ok_result = HealthCheckResult(
            success=True,
            check_type="http",
            duration=0.1,
        )

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
            MockMgr.return_value.check_with_retries.side_effect = [
                fail_result,
                ok_result,
            ]
            result = deployer.execute()

        assert result.success is False
        assert result.status == DeploymentStatus.ROLLED_BACK
        assert "Health check failed" in result.error_message

    def test_execute_succeeds_when_health_check_passes(self):
        deployer = _make_deployer()
        ok_result = HealthCheckResult(success=True, check_type="http", duration=0.2)

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
            MockMgr.return_value.check_with_retries.return_value = ok_result
            result = deployer.execute()

        assert result.success is True
        assert result.status == DeploymentStatus.SUCCESS


class TestHealthCheckSkipped:
    """Test health check skipped when not configured."""

    def test_no_health_check_url_skips_check(self):
        deployer = _make_deployer(health_check={})

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch("subprocess.run"),
        ):
            result = deployer.execute()

        assert result.success is True

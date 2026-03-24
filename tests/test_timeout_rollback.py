"""Tests for rollback-on-timeout in APIDeployer."""

from unittest.mock import MagicMock, patch

from fraisier.deployers.api import APIDeployer, _DeploymentTimeout
from fraisier.deployers.base import DeploymentResult, DeploymentStatus


def _make_api_deployer(**overrides) -> APIDeployer:
    config = {
        "fraise_name": "my_api",
        "environment": "production",
        "app_path": "/tmp/test-api",
        "branch": "main",
        "timeout": 5,
        **overrides,
    }
    runner = MagicMock()
    return APIDeployer(config, runner=runner)


class TestTimeoutRollback:
    """Verify that timeout triggers rollback when _previous_sha is available."""

    def test_timeout_triggers_rollback_when_previous_sha_exists(self, test_db):
        """When _DeploymentTimeout fires and _previous_sha is set,
        rollback() should be called and result should be ROLLED_BACK."""
        deployer = _make_api_deployer()
        deployer._previous_sha = "abc123def456"

        rollback_result = DeploymentResult(
            success=True,
            status=DeploymentStatus.ROLLED_BACK,
            new_version="abc123de",
        )

        with (
            patch.object(deployer, "_git_pull", side_effect=_DeploymentTimeout("boom")),
            patch.object(deployer, "rollback", return_value=rollback_result) as mock_rb,
            patch.object(deployer, "_write_status"),
            patch.object(deployer, "_start_db_record", return_value=None),
        ):
            result = deployer.execute()

        mock_rb.assert_called_once()
        assert result.status == DeploymentStatus.ROLLED_BACK

    def test_timeout_without_previous_sha_returns_failed(self, test_db):
        """When _DeploymentTimeout fires and no _previous_sha, just fail."""
        deployer = _make_api_deployer()
        deployer._previous_sha = None

        with (
            patch.object(deployer, "_git_pull", side_effect=_DeploymentTimeout("boom")),
            patch.object(deployer, "_write_status"),
            patch.object(deployer, "_start_db_record", return_value=None),
        ):
            result = deployer.execute()

        assert result.status == DeploymentStatus.FAILED
        assert result.success is False

    def test_timeout_with_failed_rollback_returns_failed(self, test_db):
        """When rollback also fails after timeout, result should be FAILED."""
        deployer = _make_api_deployer()
        deployer._previous_sha = "abc123def456"

        rollback_result = DeploymentResult(
            success=False,
            status=DeploymentStatus.FAILED,
            error_message="rollback broke",
        )

        with (
            patch.object(deployer, "_git_pull", side_effect=_DeploymentTimeout("boom")),
            patch.object(deployer, "rollback", return_value=rollback_result),
            patch.object(deployer, "_write_status"),
            patch.object(deployer, "_start_db_record", return_value=None),
        ):
            result = deployer.execute()

        assert result.status == DeploymentStatus.FAILED
        assert "rollback" in (result.error_message or "").lower()

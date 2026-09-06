"""Tests for rollback-on-timeout in APIDeployer.

These used to patch ``_write_status`` out, so what the timeout path recorded
was never observed — and it recorded the wrong thing for years (#378). The
status file is now real and asserted against the returned result.
"""

from unittest.mock import MagicMock, patch

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentResult, DeploymentStatus
from fraisier.status import read_status
from fraisier.timeout import DeploymentTimeoutExpired


def _make_api_deployer(tmp_path, **overrides) -> APIDeployer:
    config = {
        "fraise_name": "my_api",
        "environment": "production",
        "app_path": "/tmp/test-api",
        "branch": "main",
        "timeout": 5,
        "status_dir": str(tmp_path / "status"),
        **overrides,
    }
    runner = MagicMock()
    return APIDeployer(config, runner=runner)


def _assert_record_matches(result, tmp_path):
    """The record equals the returned result; hand it back for further checks."""
    status = read_status("my_api", status_dir=tmp_path / "status")
    assert status is not None, "the timeout path filed no record"
    assert status.state == result.status.value, (
        f"returned {result.status.value!r} and filed {status.state!r}"
    )
    return status


class TestTimeoutRollback:
    """Verify that timeout triggers rollback when _previous_sha is available."""

    def test_timeout_triggers_rollback_when_previous_sha_exists(
        self, test_db, tmp_path
    ):
        """A timeout with a previous SHA rolls back and reports ROLLED_BACK.

        The real ``rollback()`` runs: it is the code that files the record, so
        a stub of it would leave nothing to assert against.
        """
        deployer = _make_api_deployer(tmp_path)
        deployer._previous_sha = "abc123def456"

        with (
            patch.object(
                deployer,
                "_git_pull",
                side_effect=DeploymentTimeoutExpired("boom"),
            ),
            patch.object(deployer, "_git_rollback") as mock_git_rollback,
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
            patch.object(deployer, "_start_db_record", return_value=None),
        ):
            result = deployer.execute()

        mock_git_rollback.assert_called_once_with("abc123def456")
        assert result.status == DeploymentStatus.ROLLED_BACK
        _assert_record_matches(result, tmp_path)

    def test_timeout_without_previous_sha_returns_failed(self, test_db, tmp_path):
        """When _DeploymentTimeout fires and no _previous_sha, just fail."""
        deployer = _make_api_deployer(tmp_path)
        deployer._previous_sha = None

        with (
            patch.object(
                deployer,
                "_git_pull",
                side_effect=DeploymentTimeoutExpired("boom"),
            ),
            patch.object(deployer, "_start_db_record", return_value=None),
        ):
            result = deployer.execute()

        assert result.status == DeploymentStatus.FAILED
        assert result.success is False
        _assert_record_matches(result, tmp_path)

    def test_timeout_with_failed_rollback_returns_rollback_failed(
        self, test_db, tmp_path
    ):
        """When rollback also fails after timeout, result is ROLLBACK_FAILED."""
        deployer = _make_api_deployer(tmp_path)
        deployer._previous_sha = "abc123def456"

        rollback_result = DeploymentResult(
            success=False,
            status=DeploymentStatus.FAILED,
            error_message="rollback broke",
        )

        with (
            patch.object(
                deployer,
                "_git_pull",
                side_effect=DeploymentTimeoutExpired("boom"),
            ),
            patch.object(deployer, "rollback", return_value=rollback_result),
            patch.object(deployer, "_start_db_record", return_value=None),
        ):
            result = deployer.execute()

        assert result.status == DeploymentStatus.ROLLBACK_FAILED
        assert "rollback" in (result.error_message or "").lower()
        status = _assert_record_matches(result, tmp_path)
        assert "rollback broke" in (status.error_message or ""), (
            "the timeout message overwrote the rollback's own explanation: "
            f"{status.error_message!r}"
        )

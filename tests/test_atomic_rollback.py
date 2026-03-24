"""Tests for atomic migration rollback — exact step tracking."""

from unittest.mock import MagicMock, patch

from fraisier.deployers.api import APIDeployer
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
        "database": {
            "strategy": "migrate",
            "confiture_config": "confiture.yaml",
        },
        "repos_base": str(tmp_path / "repos"),
        "status_dir": str(tmp_path / "status"),
        **overrides,
    }
    return APIDeployer(config)


class TestRollbackExactSteps:
    """Rollback undoes exactly the number of migrations that were applied."""

    def test_rollback_passes_exact_applied_count(self, tmp_path):
        """If 3 migrations were applied, rollback calls strategy with steps=3."""
        deployer = _make_deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 3

        mock_strategy = MagicMock()
        mock_strategy.rollback.return_value = StrategyResult(
            success=True, migrations_applied=3
        )

        with (
            patch(
                "fraisier.strategies.get_strategy",
                return_value=mock_strategy,
            ),
            patch("subprocess.run"),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            result = deployer.rollback()

        mock_strategy.rollback.assert_called_once()
        call_kwargs = mock_strategy.rollback.call_args
        assert call_kwargs.kwargs["steps"] == 3
        assert result.success is True

    def test_rollback_skips_db_when_zero_applied(self, tmp_path):
        """If 0 migrations were applied, DB rollback is skipped entirely."""
        deployer = _make_deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 0

        mock_strategy = MagicMock()

        with (
            patch(
                "fraisier.strategies.get_strategy",
                return_value=mock_strategy,
            ),
            patch("subprocess.run"),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            result = deployer.rollback()

        mock_strategy.rollback.assert_not_called()
        assert result.success is True


class TestPartialRollbackIncidentFile:
    """When rollback partially fails, incident file records exact state."""

    def test_partial_rollback_records_applied_and_remaining(self, tmp_path):
        """If 3 applied and rollback undoes 2, incident records 1 remaining."""
        deployer = _make_deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 3

        mock_strategy = MagicMock()
        mock_strategy.rollback.return_value = StrategyResult(
            success=False,
            migrations_applied=2,
            errors=["down migration 001 failed: constraint violation"],
        )

        with (
            patch(
                "fraisier.strategies.get_strategy",
                return_value=mock_strategy,
            ),
            patch("subprocess.run"),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
            patch.object(deployer, "_write_incident") as mock_incident,
        ):
            result = deployer.rollback()

        assert not result.success
        # Incident file should record the partial state
        mock_incident.assert_called_once()
        call_kwargs = mock_incident.call_args
        incident_msg = call_kwargs.args[0]
        # Should mention how many were rolled back and how many remain
        assert "2" in incident_msg  # rolled back
        assert "1" in incident_msg  # still applied

    def test_full_rollback_failure_records_all_remaining(self, tmp_path):
        """If 3 applied and rollback undoes 0, incident records 3 remaining."""
        deployer = _make_deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 3

        mock_strategy = MagicMock()
        mock_strategy.rollback.return_value = StrategyResult(
            success=False,
            migrations_applied=0,
            errors=["down migration 003 failed: file not found"],
        )

        with (
            patch(
                "fraisier.strategies.get_strategy",
                return_value=mock_strategy,
            ),
            patch("subprocess.run"),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
            patch.object(deployer, "_write_incident") as mock_incident,
        ):
            result = deployer.rollback()

        assert not result.success
        mock_incident.assert_called_once()
        call_kwargs = mock_incident.call_args
        incident_msg = call_kwargs.args[0]
        assert "3" in incident_msg  # all still applied

    def test_migrations_applied_in_result_details(self, tmp_path):
        """DeploymentResult.details includes migrations_applied count."""
        deployer = _make_deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 5

        mock_strategy = MagicMock()
        mock_strategy.rollback.return_value = StrategyResult(
            success=True, migrations_applied=5
        )

        with (
            patch(
                "fraisier.strategies.get_strategy",
                return_value=mock_strategy,
            ),
            patch("subprocess.run"),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
        ):
            result = deployer.rollback()

        assert result.success
        assert result.details.get("migrations_rolled_back") == 5

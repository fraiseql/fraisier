"""Integration tests for deploy → rollback with real git operations.

Uses real git bare repo + worktree from git_deploy_env fixture.
Mocks only systemctl (can't restart real services) and health checks
(no real service to check).
"""

from unittest.mock import MagicMock, patch

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentStatus
from fraisier.runners import LocalRunner
from fraisier.strategies import StrategyResult
from tests.fixtures.git_env import DeployEnv


def _make_deployer(
    env: DeployEnv,
    *,
    health_check_url: str | None = "http://localhost:8000/health",
    with_database: bool = False,
) -> APIDeployer:
    """Create an APIDeployer wired to the real git_deploy_env.

    Uses a real LocalRunner so git commands execute for real.
    Only systemctl and health checks need mocking.
    """
    config = {
        "fraise_name": "test_api",
        "environment": "production",
        "app_path": str(env.worktree),
        "branch": "main",
        "repos_base": str(env.bare_repo.parent),
        "status_dir": str(env.status_dir),
        "systemd_service": "test-api.service",
    }
    if health_check_url:
        config["health_check"] = {
            "url": health_check_url,
            "timeout": 5,
            "retries": 1,
        }
    if with_database:
        config["database"] = {
            "strategy": "migrate",
            "confiture_config": "confiture.yaml",
            "migrations_dir": "db/migrations",
        }

    deployer = APIDeployer(config, runner=LocalRunner())
    # Override bare_repo to match our fixture (repos_base/test_api.git)
    deployer.bare_repo = env.bare_repo
    return deployer


class TestDeployRollbackIntegration:
    """Deploy v2, then test rollback scenarios with real git."""

    def test_deploy_then_health_fail_triggers_rollback(
        self, git_deploy_env: DeployEnv, test_db
    ):
        """Deploy v2 → health check fails → rollback restores v1 code."""
        deployer = _make_deployer(git_deploy_env)

        with (
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=False),
            patch.object(deployer, "_write_status"),
        ):
            result = deployer.execute()

        assert not result.success
        assert result.status == DeploymentStatus.ROLLED_BACK

        # Verify worktree is back to v1 content
        content = (git_deploy_env.worktree / "app.py").read_text()
        assert 'VERSION = "v1"' in content

    def test_deploy_success_updates_worktree_to_v2(
        self, git_deploy_env: DeployEnv, test_db
    ):
        """Successful deploy updates worktree to v2."""
        deployer = _make_deployer(git_deploy_env)

        with (
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
            patch.object(deployer, "_write_status"),
        ):
            result = deployer.execute()

        assert result.success
        assert result.status == DeploymentStatus.SUCCESS

        content = (git_deploy_env.worktree / "app.py").read_text()
        assert 'VERSION = "v2"' in content

    def test_rollback_restores_v1_status_file(self, git_deploy_env: DeployEnv, test_db):
        """After rollback, status file shows ROLLED_BACK."""
        deployer = _make_deployer(git_deploy_env)

        status_calls = []

        def track_status(state, **kwargs):
            status_calls.append((state, kwargs))

        with (
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=False),
            patch.object(deployer, "_write_status", side_effect=track_status),
        ):
            result = deployer.execute()

        assert result.status == DeploymentStatus.ROLLED_BACK
        # Should have written: deploying → rolled_back
        states = [s[0] for s in status_calls]
        assert "deploying" in states
        assert "rolled_back" in states

    def test_deploy_with_db_rollback_on_health_fail(
        self, git_deploy_env: DeployEnv, test_db
    ):
        """Deploy with migrations → health fail → rollback undoes git + migrations."""
        deployer = _make_deployer(git_deploy_env, with_database=True)

        mock_strategy = MagicMock()
        mock_strategy.execute.return_value = StrategyResult(
            success=True, migrations_applied=2
        )
        mock_strategy.rollback.return_value = StrategyResult(
            success=True, migrations_applied=2
        )

        with (
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=False),
            patch.object(deployer, "_write_status"),
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(mock_strategy, "confiture.yaml", "db/migrations"),
            ),
        ):
            result = deployer.execute()

        assert result.status == DeploymentStatus.ROLLED_BACK

        # Verify migrations were rolled back
        mock_strategy.rollback.assert_called_once()

        # Verify worktree is back to v1
        content = (git_deploy_env.worktree / "app.py").read_text()
        assert 'VERSION = "v1"' in content

    def test_rollback_fails_when_db_rollback_fails(
        self, git_deploy_env: DeployEnv, test_db
    ):
        """Deploy v2 → migration rollback fails → ROLLBACK_FAILED + incident."""
        deployer = _make_deployer(git_deploy_env, with_database=True)

        mock_strategy = MagicMock()
        mock_strategy.execute.return_value = StrategyResult(
            success=True, migrations_applied=3
        )
        mock_strategy.rollback.return_value = StrategyResult(
            success=False,
            migrations_applied=1,
            errors=["Cannot drop column: data loss"],
        )

        incident_calls = []

        with (
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=False),
            patch.object(deployer, "_write_status"),
            patch.object(
                deployer,
                "_resolve_strategy",
                return_value=(mock_strategy, "confiture.yaml", "db/migrations"),
            ),
            patch.object(
                deployer,
                "_write_incident",
                side_effect=lambda *a, **kw: incident_calls.append((a, kw)),
            ),
        ):
            result = deployer.execute()

        assert not result.success
        assert result.status == DeploymentStatus.ROLLBACK_FAILED
        assert "manual intervention" in result.error_message.lower()

        # Incident file should have been written
        assert len(incident_calls) == 1

    def test_deploy_records_in_database(self, git_deploy_env: DeployEnv, test_db):
        """Successful deploy is recorded in the database."""
        deployer = _make_deployer(git_deploy_env)

        with (
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
            patch.object(deployer, "_write_status"),
        ):
            result = deployer.execute()

        assert result.success

        deployments = test_db.get_recent_deployments(limit=1, fraise="test_api")
        assert len(deployments) == 1
        assert deployments[0]["status"] == "success"

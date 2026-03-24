"""Tests that specific exceptions propagate instead of being swallowed."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from fraisier.deployers.docker_compose import DockerComposeDeployer


class TestDockerComposeExceptionSpecificity:
    """Verify DockerComposeDeployer only catches expected errors."""

    def _make_deployer(self) -> DockerComposeDeployer:
        runner = MagicMock()
        config = {
            "fraise_name": "web",
            "environment": "prod",
            "service_name": "api",
            "compose_file": "docker-compose.yml",
        }
        return DockerComposeDeployer(config, runner=runner)

    def test_get_current_version_propagates_non_subprocess_error(self):
        """RuntimeError in get_current_version should propagate."""
        deployer = self._make_deployer()
        deployer.runner.run.side_effect = RuntimeError("unexpected")
        with pytest.raises(RuntimeError, match="unexpected"):
            deployer.get_current_version()

    def test_health_check_propagates_non_subprocess_error(self):
        """RuntimeError in health_check should propagate."""
        deployer = self._make_deployer()
        deployer.runner.run.side_effect = RuntimeError("unexpected")
        with pytest.raises(RuntimeError, match="unexpected"):
            deployer.health_check()

    def test_get_current_version_still_catches_subprocess_error(self):
        """subprocess.CalledProcessError should still return None."""
        import subprocess

        deployer = self._make_deployer()
        deployer.runner.run.side_effect = subprocess.CalledProcessError(1, "docker")
        assert deployer.get_current_version() is None

    def test_health_check_still_catches_subprocess_error(self):
        """subprocess.CalledProcessError should still return False."""
        import subprocess

        deployer = self._make_deployer()
        deployer.runner.run.side_effect = subprocess.CalledProcessError(1, "docker")
        assert deployer.health_check() is False


class TestMixinsExceptionSpecificity:
    """Verify mixin DB recording only catches DB-related errors."""

    def _make_deployer(self):
        """Create a deployer that uses GitDeployMixin."""
        from fraisier.deployers.etl import ETLDeployer

        config = {
            "fraise_name": "pipeline",
            "environment": "prod",
            "app_path": "/tmp/test",
            "branch": "main",
        }
        runner = MagicMock()
        return ETLDeployer(config, runner=runner)

    def test_start_db_record_propagates_non_db_error(self, test_db):
        """RuntimeError in _start_db_record should propagate."""
        deployer = self._make_deployer()
        with (
            patch.object(
                test_db, "start_deployment", side_effect=RuntimeError("unexpected")
            ),
            pytest.raises(RuntimeError, match="unexpected"),
        ):
            deployer._start_db_record()

    def test_complete_db_record_propagates_non_db_error(self, test_db):
        """RuntimeError in _complete_db_record should propagate."""
        from fraisier.deployers.base import DeploymentResult, DeploymentStatus

        deployer = self._make_deployer()
        result = DeploymentResult(success=True, status=DeploymentStatus.SUCCESS)
        with (
            patch.object(
                test_db, "complete_deployment", side_effect=RuntimeError("unexpected")
            ),
            pytest.raises(RuntimeError, match="unexpected"),
        ):
            deployer._complete_db_record(1, result)

    def test_start_db_record_still_catches_sqlite_error(self, test_db):
        """sqlite3.Error in _start_db_record should be caught (returns None)."""
        deployer = self._make_deployer()
        with patch.object(
            test_db,
            "start_deployment",
            side_effect=sqlite3.OperationalError("db locked"),
        ):
            assert deployer._start_db_record() is None

    def test_complete_db_record_still_catches_sqlite_error(self, test_db):
        """sqlite3.Error in _complete_db_record should be caught (no raise)."""
        from fraisier.deployers.base import DeploymentResult, DeploymentStatus

        deployer = self._make_deployer()
        result = DeploymentResult(success=True, status=DeploymentStatus.SUCCESS)
        with patch.object(
            test_db,
            "complete_deployment",
            side_effect=sqlite3.OperationalError("db locked"),
        ):
            deployer._complete_db_record(1, result)  # should not raise

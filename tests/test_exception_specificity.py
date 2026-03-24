"""Tests that specific exceptions propagate instead of being swallowed.

Phase 1, Cycle 1: Verifies that broad except-Exception catches have been
replaced with specific types so unexpected errors bubble up.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from fraisier.deployers.docker_compose import DockerComposeDeployer
from fraisier.locking import DeploymentLock


class TestDeploymentLockExceptionSpecificity:
    """Verify DeploymentLock only catches DB-related errors, not all exceptions."""

    def test_acquire_propagates_non_db_error(self, test_db):
        """RuntimeError from DB layer should propagate, not be swallowed."""
        lock = DeploymentLock("svc", "prov")
        with (
            patch.object(
                test_db, "get_deployment_lock", side_effect=RuntimeError("unexpected")
            ),
            pytest.raises(RuntimeError, match="unexpected"),
        ):
            lock.acquire()

    def test_release_propagates_non_db_error(self, test_db):
        """RuntimeError during release should propagate."""
        lock = DeploymentLock("svc", "prov")
        lock._is_locked = True
        with (
            patch.object(
                test_db,
                "release_deployment_lock",
                side_effect=RuntimeError("unexpected"),
            ),
            pytest.raises(RuntimeError, match="unexpected"),
        ):
            lock.release()

    def test_is_locked_propagates_non_db_error(self, test_db):
        """RuntimeError from is_locked check should propagate."""
        with (
            patch.object(
                test_db, "get_deployment_lock", side_effect=RuntimeError("unexpected")
            ),
            pytest.raises(RuntimeError, match="unexpected"),
        ):
            DeploymentLock.is_locked("svc", "prov")

    def test_get_lock_info_propagates_non_db_error(self, test_db):
        """RuntimeError from get_lock_info should propagate."""
        with (
            patch.object(
                test_db, "get_deployment_lock", side_effect=RuntimeError("unexpected")
            ),
            pytest.raises(RuntimeError, match="unexpected"),
        ):
            DeploymentLock.get_lock_info("svc", "prov")

    def test_clear_lock_propagates_non_db_error(self, test_db):
        """RuntimeError from clear_lock should propagate."""
        with (
            patch.object(
                test_db,
                "release_deployment_lock",
                side_effect=RuntimeError("unexpected"),
            ),
            pytest.raises(RuntimeError, match="unexpected"),
        ):
            DeploymentLock.clear_lock("svc", "prov")

    def test_acquire_still_catches_sqlite_error(self, test_db):
        """sqlite3.Error should still be caught (returns False)."""
        lock = DeploymentLock("svc", "prov")
        with patch.object(
            test_db,
            "get_deployment_lock",
            side_effect=sqlite3.OperationalError("db locked"),
        ):
            assert lock.acquire() is False

    def test_release_still_catches_sqlite_error(self, test_db):
        """sqlite3.Error during release should be caught (no raise)."""
        lock = DeploymentLock("svc", "prov")
        lock._is_locked = True
        with patch.object(
            test_db,
            "release_deployment_lock",
            side_effect=sqlite3.OperationalError("db locked"),
        ):
            lock.release()  # should not raise
            assert lock._is_locked is False


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

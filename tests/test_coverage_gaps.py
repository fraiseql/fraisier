"""Tests for previously untested public functions.

Targets the highest-risk coverage gaps identified by audit:
- DeploymentLockStore (acquire/release/get)
- DatabaseDeploymentLock (acquire/release/context manager)
- Config.reload() and filter methods
- BaseDeployer.is_deployment_needed()
- FraisierDB lock delegation methods
"""

from datetime import datetime, timedelta

import pytest

from fraisier.config import FraisierConfig
from fraisier.deployers.base import BaseDeployer, DeploymentResult, DeploymentStatus
from fraisier.errors import DeploymentLockError
from fraisier.locking import DatabaseDeploymentLock


class TestDeploymentLockStore:
    """Test database-backed lock store methods."""

    def test_acquire_and_get_lock(self, test_db):
        """Acquire a lock, then verify it's visible via get."""
        expires = (datetime.now() + timedelta(hours=1)).isoformat()
        test_db.acquire_deployment_lock("my_api", "bare_metal", expires)

        lock = test_db.get_deployment_lock("my_api", "bare_metal")
        assert lock is not None
        assert lock["service_name"] == "my_api"
        assert lock["provider_name"] == "bare_metal"

    def test_get_nonexistent_lock_returns_none(self, test_db):
        """Getting a lock that doesn't exist returns None."""
        lock = test_db.get_deployment_lock("nonexistent", "bare_metal")
        assert lock is None

    def test_release_lock(self, test_db):
        """Release a lock, then verify it's gone."""
        expires = (datetime.now() + timedelta(hours=1)).isoformat()
        test_db.acquire_deployment_lock("my_api", "bare_metal", expires)
        test_db.release_deployment_lock("my_api", "bare_metal")

        lock = test_db.get_deployment_lock("my_api", "bare_metal")
        assert lock is None

    def test_release_nonexistent_lock_is_noop(self, test_db):
        """Releasing a non-existent lock doesn't raise."""
        test_db.release_deployment_lock("nonexistent", "bare_metal")

    def test_acquire_with_datetime_object(self, test_db):
        """Acquire lock with a datetime object (not string)."""
        expires = datetime.now() + timedelta(hours=1)
        test_db.acquire_deployment_lock("my_api", "bare_metal", expires)

        lock = test_db.get_deployment_lock("my_api", "bare_metal")
        assert lock is not None

    def test_duplicate_lock_raises(self, test_db):
        """Acquiring the same lock twice raises (UNIQUE constraint)."""
        expires = (datetime.now() + timedelta(hours=1)).isoformat()
        test_db.acquire_deployment_lock("my_api", "bare_metal", expires)

        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            test_db.acquire_deployment_lock("my_api", "bare_metal", expires)


class TestDatabaseDeploymentLock:
    """Test the SQLite-backed distributed lock."""

    def test_acquire_and_release(self, tmp_path):
        """Acquire then release a lock."""
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path)

        assert lock.acquire("my_api") is True
        lock.release("my_api")

    def test_double_acquire_fails(self, tmp_path):
        """Cannot acquire the same lock twice."""
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path)

        assert lock.acquire("my_api") is True
        assert lock.acquire("my_api") is False

    def test_acquire_after_release_succeeds(self, tmp_path):
        """Can acquire lock after releasing it."""
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path)

        lock.acquire("my_api")
        lock.release("my_api")
        assert lock.acquire("my_api") is True

    def test_stale_lock_reclaimed(self, tmp_path):
        """Stale lock (older than TTL) is automatically reclaimed."""
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path, ttl=0)

        assert lock.acquire("my_api") is True
        # TTL=0 means it's immediately stale
        assert lock.acquire("my_api") is True

    def test_context_manager_acquires_and_releases(self, tmp_path):
        """Context manager acquires on entry, releases on exit."""
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path)

        with lock("my_api"):
            # Lock should be held
            assert lock.acquire("my_api") is False

        # Lock should be released
        assert lock.acquire("my_api") is True

    def test_context_manager_raises_if_locked(self, tmp_path):
        """Context manager raises DeploymentLockError if already locked."""
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path)

        lock.acquire("my_api")
        with pytest.raises(DeploymentLockError), lock("my_api"):
            pass


class TestConfigReload:
    """Test config reload and filtering methods."""

    def test_reload_picks_up_changes(self, tmp_path):
        """Reload reflects changes to the YAML file."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("""
fraises:
  api_v1:
    type: api
    description: V1 API
    environments:
      production:
        app_path: /tmp/api
""")
        config = FraisierConfig(str(config_file))
        assert "api_v1" in config.fraises

        config_file.write_text("""
fraises:
  api_v2:
    type: api
    description: V2 API
    environments:
      production:
        app_path: /tmp/api
""")
        config.reload()
        assert "api_v2" in config.fraises
        assert "api_v1" not in config.fraises

    def test_get_deployments_by_type(self, tmp_path):
        """Filter deployments by fraise type."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("""
fraises:
  my_api:
    type: api
    description: API
    environments:
      prod:
        app_path: /tmp/api
  my_etl:
    type: etl
    description: ETL
    environments:
      prod:
        app_path: /tmp/etl
""")
        config = FraisierConfig(str(config_file))

        api_deps = config.get_deployments_by_type("api")
        assert all(d["type"] == "api" for d in api_deps)
        assert len(api_deps) == 1

        etl_deps = config.get_deployments_by_type("etl")
        assert len(etl_deps) == 1

    def test_get_deployments_by_environment(self, tmp_path):
        """Filter deployments by environment."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("""
fraises:
  my_api:
    type: api
    description: API
    environments:
      prod:
        app_path: /tmp/api-prod
      staging:
        app_path: /tmp/api-staging
""")
        config = FraisierConfig(str(config_file))

        prod = config.get_deployments_by_environment("prod")
        assert len(prod) == 1
        assert prod[0]["environment"] == "prod"

    def test_list_environments(self, tmp_path):
        """List environments for a specific fraise."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("""
fraises:
  my_api:
    type: api
    description: API
    environments:
      production:
        app_path: /tmp/prod
      staging:
        app_path: /tmp/staging
""")
        config = FraisierConfig(str(config_file))

        envs = config.list_environments("my_api")
        assert set(envs) == {"production", "staging"}

    def test_list_environments_nonexistent_fraise(self, tmp_path):
        """Listing environments for non-existent fraise returns empty."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("fraises: {}")
        config = FraisierConfig(str(config_file))

        assert config.list_environments("nonexistent") == []

    def test_get_git_provider_config(self, tmp_path):
        """Get git provider configuration section."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("""
git:
  provider: github
  github:
    webhook_secret: secret123
fraises: {}
""")
        config = FraisierConfig(str(config_file))

        git_config = config.get_git_provider_config()
        assert git_config["provider"] == "github"
        assert "github" in git_config


class TestIsDeploymentNeeded:
    """Test version comparison logic in BaseDeployer."""

    def _make_deployer(self, current, latest):
        """Create a concrete deployer with stubbed versions."""

        class StubDeployer(BaseDeployer):
            def get_current_version(self):
                return current

            def get_latest_version(self):
                return latest

            def execute(self):
                return DeploymentResult(success=True, status=DeploymentStatus.SUCCESS)

        return StubDeployer({"fraise_name": "test", "environment": "prod"})

    def test_needed_when_versions_differ(self):
        """Deployment needed when current != latest."""
        deployer = self._make_deployer("v1", "v2")
        assert deployer.is_deployment_needed() is True

    def test_not_needed_when_versions_match(self):
        """Deployment not needed when current == latest."""
        deployer = self._make_deployer("v1", "v1")
        assert deployer.is_deployment_needed() is False

    def test_needed_when_current_is_none(self):
        """Deployment needed when no current version (first deploy)."""
        deployer = self._make_deployer(None, "v1")
        assert deployer.is_deployment_needed() is True

    def test_needed_when_latest_is_none(self):
        """Deployment needed when latest is unknown."""
        deployer = self._make_deployer("v1", None)
        assert deployer.is_deployment_needed() is True

    def test_needed_when_both_none(self):
        """Deployment needed when both versions unknown."""
        deployer = self._make_deployer(None, None)
        assert deployer.is_deployment_needed() is True

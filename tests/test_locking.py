"""Tests for fraisier.locking — database-backed deployment locks."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from fraisier.locking import DeploymentLock, DeploymentLockedError


@pytest.fixture
def mock_db():
    """Mock database returned by get_db()."""
    db = MagicMock()
    db.get_deployment_lock.return_value = None
    db.acquire_deployment_lock.return_value = None
    db.release_deployment_lock.return_value = None
    with patch("fraisier.locking.get_db", return_value=db):
        yield db


class TestAcquire:
    """Tests for DeploymentLock.acquire."""

    def test_acquire_success(self, mock_db):
        lock = DeploymentLock("api", "prod")
        assert lock.acquire() is True
        mock_db.acquire_deployment_lock.assert_called_once()

    def test_acquire_already_locked(self, mock_db):
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        mock_db.get_deployment_lock.return_value = {"expires_at": future}

        lock = DeploymentLock("api", "prod")
        assert lock.acquire() is False

    def test_acquire_expired_lock_succeeds(self, mock_db):
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        mock_db.get_deployment_lock.return_value = {"expires_at": past}

        lock = DeploymentLock("api", "prod")
        assert lock.acquire() is True
        # Should release expired lock then acquire new one
        mock_db.release_deployment_lock.assert_called_once()
        mock_db.acquire_deployment_lock.assert_called_once()

    def test_acquire_db_failure_returns_false(self):
        with patch(
            "fraisier.locking.get_db", side_effect=RuntimeError("connection failed")
        ):
            lock = DeploymentLock("api", "prod")
            assert lock.acquire() is False


class TestRelease:
    """Tests for DeploymentLock.release."""

    def test_release_success(self, mock_db):
        lock = DeploymentLock("api", "prod")
        lock.acquire()
        lock.release()

        mock_db.release_deployment_lock.assert_called_with("api", "prod")
        assert lock._is_locked is False

    def test_release_when_not_locked(self, mock_db):
        lock = DeploymentLock("api", "prod")
        # release without acquire should be a no-op
        lock.release()
        mock_db.release_deployment_lock.assert_not_called()


class TestContextManager:
    """Tests for __enter__/__exit__."""

    def test_context_manager_success(self, mock_db):
        with DeploymentLock("api", "prod") as lock:
            assert lock._is_locked is True

        # After exiting, lock should be released
        mock_db.release_deployment_lock.assert_called_with("api", "prod")

    def test_context_manager_locked_raises(self, mock_db):
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        mock_db.get_deployment_lock.return_value = {"expires_at": future}

        with (
            pytest.raises(DeploymentLockedError, match="already locked"),
            DeploymentLock("api", "prod"),
        ):
            pass  # Should not reach here


class TestStaticMethods:
    """Tests for is_locked, get_lock_info, clear_lock."""

    def test_is_locked_true(self, mock_db):
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        mock_db.get_deployment_lock.return_value = {"expires_at": future}

        assert DeploymentLock.is_locked("api", "prod") is True

    def test_is_locked_false(self, mock_db):
        mock_db.get_deployment_lock.return_value = None

        assert DeploymentLock.is_locked("api", "prod") is False

    def test_is_locked_expired(self, mock_db):
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        mock_db.get_deployment_lock.return_value = {"expires_at": past}

        assert DeploymentLock.is_locked("api", "prod") is False

    def test_clear_lock(self, mock_db):
        assert DeploymentLock.clear_lock("api", "prod") is True
        mock_db.release_deployment_lock.assert_called_with("api", "prod")

    def test_get_lock_info(self, mock_db):
        mock_db.get_deployment_lock.return_value = {
            "expires_at": "2026-01-01T00:00:00+00:00",
            "locked_at": "2025-12-31T23:55:00+00:00",
        }

        info = DeploymentLock.get_lock_info("api", "prod")
        assert info is not None
        assert info["service_name"] == "api"
        assert info["provider_name"] == "prod"
        assert info["expires_at"] == "2026-01-01T00:00:00+00:00"

    def test_get_lock_info_none(self, mock_db):
        mock_db.get_deployment_lock.return_value = None
        assert DeploymentLock.get_lock_info("api", "prod") is None

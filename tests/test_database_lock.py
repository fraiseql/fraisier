"""Tests for database-backed deployment locking."""

import os
import socket
import sqlite3
import time

import pytest

from fraisier.errors import DeploymentLockError
from fraisier.locking import DatabaseDeploymentLock


class TestDatabaseDeploymentLockAcquire:
    """Test lock acquisition inserts a row and returns True."""

    def test_acquire_inserts_lock_row_returns_true(self, tmp_path):
        lock = DatabaseDeploymentLock(db_path=tmp_path / "locks.db")
        assert lock.acquire("myfraise") is True

        # Verify row exists
        conn = sqlite3.connect(tmp_path / "locks.db")
        row = conn.execute(
            "SELECT fraise, holder FROM deployment_locks WHERE fraise = ?",
            ("myfraise",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "myfraise"

    def test_acquire_holder_contains_hostname_and_pid(self, tmp_path):
        lock = DatabaseDeploymentLock(db_path=tmp_path / "locks.db")
        lock.acquire("myfraise")

        conn = sqlite3.connect(tmp_path / "locks.db")
        row = conn.execute(
            "SELECT holder FROM deployment_locks WHERE fraise = ?",
            ("myfraise",),
        ).fetchone()
        conn.close()

        holder = row[0]
        assert socket.gethostname() in holder
        assert str(os.getpid()) in holder


class TestDatabaseDeploymentLockContention:
    """Test that second acquire while first held returns False."""

    def test_second_acquire_returns_false(self, tmp_path):
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path=db_path)

        assert lock.acquire("myfraise") is True
        assert lock.acquire("myfraise") is False

    def test_different_fraises_dont_conflict(self, tmp_path):
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path=db_path)

        assert lock.acquire("fraise_a") is True
        assert lock.acquire("fraise_b") is True


class TestDatabaseDeploymentLockRelease:
    """Test release removes lock row, next acquire succeeds."""

    def test_release_allows_reacquire(self, tmp_path):
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path=db_path)

        lock.acquire("myfraise")
        lock.release("myfraise")
        assert lock.acquire("myfraise") is True

    def test_release_nonexistent_lock_is_noop(self, tmp_path):
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path=db_path)
        # Should not raise
        lock.release("nonexistent")


class TestDatabaseDeploymentLockTTL:
    """Test that stale locks are reclaimed after TTL expires."""

    def test_stale_lock_is_reclaimed(self, tmp_path):
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path=db_path, ttl=1)

        lock.acquire("myfraise")

        # Manually backdate the lock to simulate expiry
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE deployment_locks SET acquired_at = ? WHERE fraise = ?",
            (time.time() - 10, "myfraise"),
        )
        conn.commit()
        conn.close()

        # Should reclaim stale lock
        assert lock.acquire("myfraise") is True

    def test_fresh_lock_is_not_reclaimed(self, tmp_path):
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path=db_path, ttl=600)

        lock.acquire("myfraise")
        # Lock is fresh, should not be reclaimed
        assert lock.acquire("myfraise") is False


class TestDatabaseDeploymentLockContextManager:
    """Test context manager interface."""

    def test_context_manager_acquires_and_releases(self, tmp_path):
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path=db_path)

        with lock("myfraise"):
            # Lock should be held
            assert lock.acquire("myfraise") is False

        # Lock should be released
        assert lock.acquire("myfraise") is True

    def test_context_manager_releases_on_exception(self, tmp_path):
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path=db_path)

        with pytest.raises(ValueError, match="boom"), lock("myfraise"):
            raise ValueError("boom")

        # Lock should be released despite exception
        assert lock.acquire("myfraise") is True

    def test_context_manager_raises_on_contention(self, tmp_path):
        db_path = tmp_path / "locks.db"
        lock = DatabaseDeploymentLock(db_path=db_path)

        lock.acquire("myfraise")
        with pytest.raises(DeploymentLockError, match="myfraise"), lock("myfraise"):
            pass

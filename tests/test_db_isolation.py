"""Tests proving database isolation between tests.

These tests verify that each test gets a fresh SQLite database,
preventing cross-test contamination.
"""

from fraisier.database import get_db, get_db_path


class TestDatabaseIsolation:
    """Prove that no test can see another test's data."""

    def test_write_data_to_db(self, test_db):
        """Write data — the next test must NOT see it."""
        test_db.update_fraise_state(
            fraise="isolation_canary",
            environment="test",
            version="v1",
        )
        state = test_db.get_fraise_state("isolation_canary", "test")
        assert state is not None

    def test_db_is_empty(self, test_db):
        """This test must not see the previous test's data."""
        state = test_db.get_fraise_state("isolation_canary", "test")
        assert state is None, "Cross-test contamination: saw data from another test"

    def test_get_db_path_never_returns_production_path(self):
        """get_db_path() must never point to /opt/fraisier in tests."""
        db_path = get_db_path()
        assert "/opt/fraisier" not in str(db_path), (
            f"get_db_path() returned production path: {db_path}"
        )

    def test_get_db_returns_isolated_instance(self, test_db):
        """get_db() must return the test DB, not a new one pointing elsewhere."""
        db = get_db()
        # Write via test_db, read via get_db() — must see the same data
        test_db.update_fraise_state(
            fraise="shared_check",
            environment="test",
            version="v1",
        )
        state = db.get_fraise_state("shared_check", "test")
        assert state is not None, "get_db() returned a different instance than test_db"

    def test_implicit_get_db_uses_tmp_path(self):
        """get_db() without test_db fixture must still use an isolated path."""
        db = get_db()
        db.update_fraise_state(
            fraise="implicit_canary",
            environment="test",
            version="v1",
        )
        state = db.get_fraise_state("implicit_canary", "test")
        assert state is not None

    def test_implicit_get_db_does_not_see_previous_test_data(self):
        """Must not see data from test_implicit_get_db_uses_tmp_path."""
        db = get_db()
        state = db.get_fraise_state("implicit_canary", "test")
        assert state is None, (
            "Cross-test contamination via get_db(): "
            "saw data from another test that used get_db() implicitly"
        )

    def test_fresh_db_has_no_deployments(self, test_db):
        """A fresh test DB has zero deployments."""
        deployments = test_db.get_recent_deployments(limit=100)
        assert len(deployments) == 0

    def test_fresh_db_has_no_webhooks(self, test_db):
        """A fresh test DB has zero webhook events."""
        webhooks = test_db.get_recent_webhooks(limit=100)
        assert len(webhooks) == 0

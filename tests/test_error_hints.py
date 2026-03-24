"""Tests for structured error output with recovery hints."""

from fraisier.errors import (
    DatabaseConnectionError,
    DatabaseError,
    DeploymentError,
    DeploymentLockError,
    DeploymentTimeoutError,
    FraisierError,
    HealthCheckError,
    ProviderConnectionError,
    RollbackError,
)


class TestRecoveryHints:
    """Test that error types include recovery hints."""

    def test_base_error_has_recovery_hint(self):
        """Test FraisierError.to_dict includes recovery_hint."""
        err = FraisierError("something failed")
        d = err.to_dict()
        assert "recovery_hint" in d

    def test_ssh_connection_refused(self):
        """Test SSH connection error has actionable hint."""
        err = ProviderConnectionError(
            "Connection refused",
            context={"host": "prod.example.com", "port": 22},
        )
        d = err.to_dict()
        hint = d["recovery_hint"]
        assert hint  # non-empty
        assert "ssh" in hint.lower() or "connect" in hint.lower()

    def test_migration_syntax_error(self):
        """Test database error with schema context has hint."""
        err = DatabaseError(
            "relation 'users' already exists",
            context={"tool": "confiture"},
        )
        d = err.to_dict()
        hint = d["recovery_hint"]
        assert hint
        assert "migration" in hint.lower() or "database" in hint.lower()

    def test_health_check_timeout(self):
        """Test health check timeout has hint about service readiness."""
        err = HealthCheckError(
            "Health check failed after 30s",
            context={"url": "http://localhost:8000/health"},
        )
        d = err.to_dict()
        hint = d["recovery_hint"]
        assert hint
        assert "health" in hint.lower() or "service" in hint.lower()

    def test_deployment_timeout(self):
        """Test deployment timeout has retry hint."""
        err = DeploymentTimeoutError("Timed out after 300s")
        d = err.to_dict()
        hint = d["recovery_hint"]
        assert hint
        assert "timeout" in hint.lower() or "retry" in hint.lower()

    def test_deployment_lock_error(self):
        """Test lock error hints about concurrent deploy."""
        err = DeploymentLockError("Could not acquire lock")
        d = err.to_dict()
        hint = d["recovery_hint"]
        assert hint
        assert "lock" in hint.lower() or "deploy" in hint.lower()

    def test_database_connection_error(self):
        """Test DB connection error hints about connectivity."""
        err = DatabaseConnectionError("could not connect to server")
        d = err.to_dict()
        hint = d["recovery_hint"]
        assert hint
        assert "database" in hint.lower() or "connect" in hint.lower()

    def test_rollback_error(self):
        """Test rollback error hints about manual recovery."""
        err = RollbackError("Rollback failed: pg_restore error")
        d = err.to_dict()
        hint = d["recovery_hint"]
        assert hint
        assert "rollback" in hint.lower() or "manual" in hint.lower()

    def test_deployment_error_generic(self):
        """Test generic deployment error has some hint."""
        err = DeploymentError("Something went wrong")
        d = err.to_dict()
        hint = d["recovery_hint"]
        assert hint

    def test_to_dict_matches_webhook_format(self):
        """Test to_dict output has same keys as webhook error format."""
        err = HealthCheckError(
            "timeout",
            context={"url": "http://localhost/health"},
        )
        d = err.to_dict()
        # Must have the same keys the webhook uses
        assert "error_type" in d
        assert "message" in d
        assert "recovery_hint" in d

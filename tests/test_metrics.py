"""Tests for Prometheus metrics recording (with dummy classes)."""

import fraisier.metrics as metrics_module
from fraisier.metrics import MetricsRecorder, get_metrics_recorder


class TestMetricsRecorder:
    """Test MetricsRecorder methods with dummy prometheus classes."""

    def setup_method(self):
        """Create a fresh recorder for each test."""
        self.recorder = MetricsRecorder()

    def test_record_deployment_start(self):
        """record_deployment_start does not raise."""
        self.recorder.record_deployment_start("bare_metal", "api")

    def test_record_deployment_complete(self):
        """record_deployment_complete does not raise."""
        self.recorder.record_deployment_complete(
            provider="bare_metal",
            fraise_type="api",
            status="success",
            duration=42.0,
        )

    def test_record_deployment_error(self):
        """record_deployment_error does not raise."""
        self.recorder.record_deployment_error(
            provider="docker_compose",
            error_type="timeout",
        )

    def test_record_rollback(self):
        """record_rollback does not raise."""
        self.recorder.record_rollback(
            provider="bare_metal",
            reason="health_check",
            duration=10.5,
        )

    def test_record_health_check(self):
        """record_health_check does not raise."""
        self.recorder.record_health_check(
            provider="bare_metal",
            check_type="http",
            status="pass",
            duration=0.5,
        )

    def test_set_provider_availability(self):
        """set_provider_availability does not raise."""
        self.recorder.set_provider_availability("bare_metal", True)
        self.recorder.set_provider_availability("bare_metal", False)

    def test_record_lock_wait(self):
        """record_lock_wait does not raise."""
        self.recorder.record_lock_wait(
            service="api",
            provider="bare_metal",
            wait_time=2.5,
        )

    def test_record_db_query(self):
        """record_db_query does not raise."""
        self.recorder.record_db_query(
            database_type="sqlite",
            operation="select",
            status="success",
            duration_seconds=0.01,
        )

    def test_record_db_error(self):
        """record_db_error does not raise."""
        self.recorder.record_db_error(
            database_type="postgresql",
            error_type="connection",
        )

    def test_get_metrics_summary(self):
        """get_metrics_summary returns dict with prometheus_available key."""
        summary = self.recorder.get_metrics_summary()
        assert "prometheus_available" in summary
        assert "message" in summary


class TestGetMetricsRecorder:
    """Test singleton accessor."""

    def test_get_metrics_recorder_singleton(self):
        """get_metrics_recorder returns the same instance on repeated calls."""
        # Reset the module-level singleton
        metrics_module._metrics_recorder = None
        first = get_metrics_recorder()
        second = get_metrics_recorder()
        assert first is second
        # Clean up
        metrics_module._metrics_recorder = None

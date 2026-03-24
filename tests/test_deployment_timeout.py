"""Tests for thread-based deployment timeout (replaces SIGALRM)."""

import threading
import time

from fraisier.timeout import DeploymentTimeoutExpired, deployment_timeout


class TestDeploymentTimeout:
    """Thread-based timeout context manager."""

    def test_normal_completion_no_interference(self):
        """Non-timeout code completes normally without being interrupted."""
        with deployment_timeout(5):
            result = 1 + 1
        assert result == 2

    def test_timeout_raises_on_expiry(self):
        """Code exceeding timeout raises DeploymentTimeoutExpired."""
        raised = threading.Event()

        try:
            with deployment_timeout(0.1):
                # Sleep longer than timeout
                time.sleep(1.0)
        except DeploymentTimeoutExpired:
            raised.set()

        assert raised.is_set(), "DeploymentTimeoutExpired was not raised"

    def test_timeout_message_includes_seconds(self):
        """Exception message includes the configured timeout value."""
        try:
            with deployment_timeout(0.1):
                time.sleep(1.0)
        except DeploymentTimeoutExpired as e:
            assert "0.1" in str(e)

    def test_timer_cancelled_on_normal_exit(self):
        """Timer is cancelled when block exits normally (no leak)."""
        with deployment_timeout(10) as ctx:
            pass
        # Timer should be cancelled — not alive
        assert not ctx.timer.is_alive()

    def test_timer_cancelled_on_exception(self):
        """Timer is cancelled even if the block raises a non-timeout error."""
        try:
            with deployment_timeout(10) as ctx:
                raise ValueError("unrelated error")
        except ValueError:
            pass
        assert not ctx.timer.is_alive()

    def test_on_timeout_callback_called(self):
        """Optional on_timeout callback is invoked when timeout fires."""
        callback_called = threading.Event()

        def on_timeout():
            callback_called.set()

        try:
            with deployment_timeout(0.1, on_timeout=on_timeout):
                time.sleep(1.0)
        except DeploymentTimeoutExpired:
            pass

        assert callback_called.is_set()

    def test_nested_timeouts_independent(self):
        """Nested timeouts don't interfere with each other."""
        with deployment_timeout(10), deployment_timeout(10):
            result = 42
        assert result == 42

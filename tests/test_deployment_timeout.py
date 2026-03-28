"""Tests for thread-based deployment timeout (replaces SIGALRM)."""

import logging
import threading
import time
from unittest.mock import patch

from fraisier.timeout import (
    DeploymentTimeoutExpired,
    _interrupt_main_thread,
    deployment_timeout,
)


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


class TestInterruptMainThread:
    """Tests for _interrupt_main_thread return value handling."""

    def test_logs_warning_when_thread_not_found(self, caplog):
        """Return value 0 means thread not found — should log warning."""
        with patch(
            "fraisier.timeout.ctypes.pythonapi.PyThreadState_SetAsyncExc",
            return_value=0,
        ):
            with caplog.at_level(logging.WARNING, logger="fraisier.timeout"):
                _interrupt_main_thread(None)

            assert any("thread not found" in r.message.lower() for r in caplog.records)

    def test_undoes_when_multiple_threads_affected(self):
        """Return value >1 means multiple threads affected — should undo."""
        with patch(
            "fraisier.timeout.ctypes.pythonapi.PyThreadState_SetAsyncExc",
        ) as mock_exc:
            mock_exc.return_value = 2
            _interrupt_main_thread(None)

            # Second call should undo (pass None as exception type)
            assert mock_exc.call_count == 2
            second_call = mock_exc.call_args_list[1]
            assert second_call[0][1] is None

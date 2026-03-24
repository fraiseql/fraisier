"""Tests for fraisier.health_check — HTTP, TCP, exec checkers and manager."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from fraisier.health_check import (
    CompositeHealthChecker,
    ExecHealthChecker,
    HealthCheckManager,
    HealthCheckResult,
    HTTPHealthChecker,
    TCPHealthChecker,
)


class TestHTTPHealthChecker:
    """Tests for HTTPHealthChecker."""

    def test_http_checker_success(self):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            checker = HTTPHealthChecker("http://localhost:8000/health")
            result = checker.check(timeout=5.0)

        assert result.success is True
        assert result.check_type == "http"
        assert "200" in (result.message or "")

    def test_http_checker_error(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=ConnectionError("refused"),
        ):
            checker = HTTPHealthChecker("http://localhost:8000/health")
            result = checker.check(timeout=5.0)

        assert result.success is False
        assert "Connection error" in (result.message or "")

    def test_http_checker_non_200(self):
        import urllib.error

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                url="http://localhost/health",
                code=503,
                msg="Service Unavailable",
                hdrs=None,
                fp=None,
            ),
        ):
            checker = HTTPHealthChecker("http://localhost:8000/health")
            result = checker.check(timeout=5.0)

        assert result.success is False
        assert "503" in (result.message or "")


class TestTCPHealthChecker:
    """Tests for TCPHealthChecker."""

    def test_tcp_checker_success(self):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 0

        with patch("socket.socket", return_value=mock_sock):
            checker = TCPHealthChecker("localhost", 5432)
            result = checker.check(timeout=5.0)

        assert result.success is True
        assert result.check_type == "tcp"

    def test_tcp_checker_refuse(self):
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 111  # ECONNREFUSED

        with patch("socket.socket", return_value=mock_sock):
            checker = TCPHealthChecker("localhost", 5432)
            result = checker.check(timeout=5.0)

        assert result.success is False

    def test_tcp_checker_timeout(self):
        mock_sock = MagicMock()
        mock_sock.connect_ex.side_effect = TimeoutError("timed out")

        with patch("socket.socket", return_value=mock_sock):
            checker = TCPHealthChecker("localhost", 5432)
            result = checker.check(timeout=1.0)

        assert result.success is False
        assert "error" in (result.message or "").lower()


class TestExecHealthChecker:
    """Tests for ExecHealthChecker."""

    def test_exec_checker_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="healthy\n", stderr=""
            )
            checker = ExecHealthChecker("/usr/bin/check-health")
            result = checker.check(timeout=5.0)

        assert result.success is True
        assert result.check_type == "exec"
        mock_run.assert_called_once()
        # Should use shlex.split, not shell
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs["shell"] is False

    def test_exec_checker_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="not healthy"
            )
            checker = ExecHealthChecker("/usr/bin/check-health")
            result = checker.check(timeout=5.0)

        assert result.success is False

    def test_exec_checker_timeout(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="check", timeout=5.0)
            checker = ExecHealthChecker("/usr/bin/check-health")
            result = checker.check(timeout=5.0)

        assert result.success is False
        assert "timeout" in (result.message or "").lower()

    def test_exec_checker_shell_false_default(self):
        checker = ExecHealthChecker("echo hello")
        assert checker.use_shell is False


class TestHealthCheckManager:
    """Tests for HealthCheckManager retry logic."""

    def test_manager_retries_on_failure(self):
        checker = MagicMock()
        fail = HealthCheckResult(
            success=False, check_type="http", duration=0.1, message="fail"
        )
        ok = HealthCheckResult(
            success=True, check_type="http", duration=0.1, message="ok"
        )
        checker.check.side_effect = [fail, fail, ok]
        checker.check_type = "http"

        with patch("time.sleep"):
            manager = HealthCheckManager(provider="test")
            result = manager.check_with_retries(
                checker, max_retries=3, initial_delay=0.01
            )

        assert result.success is True
        assert checker.check.call_count == 3

    def test_manager_succeeds_first_try(self):
        checker = MagicMock()
        ok = HealthCheckResult(
            success=True, check_type="http", duration=0.1, message="ok"
        )
        checker.check.return_value = ok
        checker.check_type = "http"

        manager = HealthCheckManager(provider="test")
        result = manager.check_with_retries(checker, max_retries=3)

        assert result.success is True
        assert checker.check.call_count == 1


    def test_manager_stops_after_max_retries(self):
        checker = MagicMock()
        fail = HealthCheckResult(
            success=False, check_type="http", duration=0.1, message="fail"
        )
        checker.check.return_value = fail
        checker.check_type = "http"

        with patch("time.sleep"):
            manager = HealthCheckManager(provider="test")
            result = manager.check_with_retries(
                checker, max_retries=4, initial_delay=0.01
            )

        assert result.success is False
        assert checker.check.call_count == 4

    def test_manager_exponential_backoff_delays(self):
        checker = MagicMock()
        fail = HealthCheckResult(
            success=False, check_type="http", duration=0.1, message="fail"
        )
        checker.check.return_value = fail
        checker.check_type = "http"

        with patch("time.sleep") as mock_sleep:
            manager = HealthCheckManager(provider="test")
            manager.check_with_retries(
                checker,
                max_retries=4,
                initial_delay=1.0,
                backoff_factor=2.0,
                max_delay=10.0,
            )

        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == [1.0, 2.0, 4.0]

    def test_manager_returns_first_success(self):
        checker = MagicMock()
        ok = HealthCheckResult(
            success=True, check_type="http", duration=0.1, message="ok"
        )
        checker.check.return_value = ok
        checker.check_type = "http"

        manager = HealthCheckManager(provider="test")
        result = manager.check_with_retries(
            checker, max_retries=5, initial_delay=0.01
        )

        assert result.success is True
        assert checker.check.call_count == 1

    @pytest.mark.asyncio
    async def test_async_check_with_retries_exponential_backoff(self):
        """Async retry uses same exponential backoff as sync version."""
        call_count = 0

        async def failing_check():
            nonlocal call_count
            call_count += 1
            return False

        with patch("asyncio.sleep") as mock_sleep:
            mock_sleep.return_value = None
            manager = HealthCheckManager(provider="test")
            result = await manager.async_check_with_retries(
                failing_check,
                max_retries=4,
                initial_delay=1.0,
                backoff_factor=2.0,
                max_delay=10.0,
            )

        assert result is False
        assert call_count == 4
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == [1.0, 2.0, 4.0]

    @pytest.mark.asyncio
    async def test_async_check_returns_first_success(self):
        call_count = 0

        async def succeeding_check():
            nonlocal call_count
            call_count += 1
            return True

        manager = HealthCheckManager(provider="test")
        result = await manager.async_check_with_retries(
            succeeding_check, max_retries=5
        )

        assert result is True
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_async_check_stops_after_max_retries(self):
        call_count = 0

        async def failing_check():
            nonlocal call_count
            call_count += 1
            return False

        with patch("asyncio.sleep") as mock_sleep:
            mock_sleep.return_value = None
            manager = HealthCheckManager(provider="test")
            result = await manager.async_check_with_retries(
                failing_check, max_retries=3, initial_delay=0.01
            )

        assert result is False
        assert call_count == 3


class TestCompositeHealthChecker:
    """Tests for CompositeHealthChecker."""

    @pytest.fixture
    def passing_checker(self):
        checker = MagicMock()
        checker.check.return_value = HealthCheckResult(
            success=True, check_type="http", duration=0.1
        )
        checker.check_type = "http"
        return checker

    @pytest.fixture
    def failing_checker(self):
        checker = MagicMock()
        checker.check.return_value = HealthCheckResult(
            success=False, check_type="tcp", duration=0.1, message="refused"
        )
        checker.check_type = "tcp"
        return checker

    def test_composite_all_pass(self, passing_checker):
        composite = CompositeHealthChecker()
        composite.add_check("http", passing_checker)
        composite.add_check("http2", passing_checker)

        success, results = composite.check_all(require_all=True)
        assert success is True
        assert len(results) == 2

    def test_composite_partial_fail_require_all(self, passing_checker, failing_checker):
        composite = CompositeHealthChecker()
        composite.add_check("http", passing_checker)
        composite.add_check("tcp", failing_checker)

        success, _results = composite.check_all(require_all=True)
        assert success is False

    def test_composite_partial_fail_any(self, passing_checker, failing_checker):
        composite = CompositeHealthChecker()
        composite.add_check("http", passing_checker)
        composite.add_check("tcp", failing_checker)

        success, _results = composite.check_all(require_all=False)
        assert success is True

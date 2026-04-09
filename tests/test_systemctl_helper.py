"""Tests for fraisier.systemctl_helper — root-privileged systemctl helper."""

from __future__ import annotations

import json
import socket as _socket
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from fraisier.systemctl_helper import (
    _ALLOWED_ACTIONS,
    _build_server_socket,
    _handle_connection,
    _send_error,
    _send_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_socket_pair() -> tuple:
    """Return a connected (server_conn, client_conn) Unix socket pair."""
    import socket as _socket

    server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    return server, client


def _recv_json(sock) -> dict:
    """Read one JSON line from *sock* and return the parsed object."""
    with sock.makefile("rb") as f:
        raw = f.readline()
    return json.loads(raw.decode())


# ---------------------------------------------------------------------------
# Allowlist validation
# ---------------------------------------------------------------------------


class TestAllowedActions:
    """_ALLOWED_ACTIONS contains the expected set of actions."""

    def test_contains_stop(self):
        assert "stop" in _ALLOWED_ACTIONS

    def test_contains_start(self):
        assert "start" in _ALLOWED_ACTIONS

    def test_contains_restart(self):
        assert "restart" in _ALLOWED_ACTIONS

    def test_contains_is_active(self):
        assert "is-active" in _ALLOWED_ACTIONS

    def test_contains_daemon_reload(self):
        assert "daemon-reload" in _ALLOWED_ACTIONS

    def test_does_not_contain_enable(self):
        assert "enable" not in _ALLOWED_ACTIONS

    def test_does_not_contain_disable(self):
        assert "disable" not in _ALLOWED_ACTIONS


# ---------------------------------------------------------------------------
# _send_response / _send_error
# ---------------------------------------------------------------------------


class TestSendResponse:
    """_send_response writes a JSON line to the socket."""

    def test_sends_json_line(self):
        server, client = _make_socket_pair()
        _send_response(server, {"ok": True, "returncode": 0})
        server.close()
        data = _recv_json(client)
        client.close()
        assert data == {"ok": True, "returncode": 0}

    def test_send_error_includes_ok_false(self):
        server, client = _make_socket_pair()
        _send_error(server, "boom")
        server.close()
        data = _recv_json(client)
        client.close()
        assert data == {"ok": False, "error": "boom"}


# ---------------------------------------------------------------------------
# _handle_connection
# ---------------------------------------------------------------------------


class TestHandleConnection:
    """_handle_connection processes one JSON request per connection."""

    def _call(self, request: dict, allowed: frozenset[str]) -> dict:
        """Send *request* via socket pair, call handler, return parsed response."""
        import socket as _socket

        server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)

        # Write request to client side, then close write direction
        client.sendall(json.dumps(request).encode() + b"\n")
        client.shutdown(_socket.SHUT_WR)

        _handle_connection(server, allowed)

        # Read response from client
        with client.makefile("rb") as f:
            raw = f.readline()
        client.close()
        return json.loads(raw.decode()) if raw else {}

    def test_rejects_unknown_action(self):
        result = self._call({"action": "enable", "service": "foo.service"}, frozenset())
        assert result["ok"] is False
        assert "action not allowed" in result["error"]

    def test_rejects_service_not_in_allowlist(self):
        result = self._call(
            {"action": "stop", "service": "evil.service"},
            frozenset({"good.service"}),
        )
        assert result["ok"] is False
        assert "service not allowed" in result["error"]

    def test_rejects_missing_service_field(self):
        result = self._call({"action": "stop"}, frozenset({"good.service"}))
        assert result["ok"] is False
        assert "missing 'service'" in result["error"]

    def test_daemon_reload_needs_no_service(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = self._call({"action": "daemon-reload"}, frozenset())

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["/usr/bin/systemctl", "daemon-reload"]
        assert result["ok"] is True

    def test_stop_allowed_service_calls_systemctl(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = self._call(
                {"action": "stop", "service": "api.service"},
                frozenset({"api.service"}),
            )

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["/usr/bin/systemctl", "stop", "api.service"]
        assert result["ok"] is True
        assert result["returncode"] == 0

    def test_systemctl_failure_returns_ok_false(self):
        mock_result = MagicMock()
        mock_result.returncode = 5
        mock_result.stdout = ""
        mock_result.stderr = "Unit not found"

        with patch("subprocess.run", return_value=mock_result):
            result = self._call(
                {"action": "restart", "service": "api.service"},
                frozenset({"api.service"}),
            )

        assert result["ok"] is False
        assert result["returncode"] == 5

    def test_malformed_json_is_handled_gracefully(self):
        import socket as _socket

        server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.sendall(b"not valid json\n")
        client.shutdown(_socket.SHUT_WR)

        # Should not raise
        _handle_connection(server, frozenset())
        client.close()

    def test_empty_connection_is_handled_gracefully(self):
        import socket as _socket

        server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.shutdown(_socket.SHUT_WR)

        # Should not raise
        _handle_connection(server, frozenset())
        client.close()

    def test_send_response_swallows_oserror(self):
        """_send_response catches OSError and logs without raising."""
        server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
        # Shutdown the client end so sendall on server will fail
        client.close()
        # Should not raise, just log warning
        _send_response(server, {"ok": True})
        server.close()

    def test_handle_connection_swallows_recv_oserror(self):
        """_handle_connection catches recv() OSError and returns gracefully."""
        mock_sock = MagicMock()
        mock_sock.recv.side_effect = OSError("Network error")
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        # Should not raise
        _handle_connection(mock_sock, frozenset({"test.service"}))

    def test_handle_connection_handles_timeout_expired(self):
        """_handle_connection catches TimeoutExpired and sends error response."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=[], timeout=120),
        ):
            result = self._call(
                {"action": "restart", "service": "api.service"},
                frozenset({"api.service"}),
            )

        assert result["ok"] is False
        assert "timed out" in result["error"]

    def test_handle_connection_handles_subprocess_oserror(self):
        """_handle_connection catches OSError from subprocess.run."""
        with patch("subprocess.run", side_effect=OSError("no such file")):
            result = self._call(
                {"action": "restart", "service": "api.service"},
                frozenset({"api.service"}),
            )

        assert result["ok"] is False
        assert "failed to run systemctl" in result["error"]


# ---------------------------------------------------------------------------
# Integration: validate allowed_services are checked correctly
# ---------------------------------------------------------------------------


class TestBuildServerSocket:
    """Test _build_server_socket initialization."""

    def test_main_exits_on_no_listen_fds(self):
        """_build_server_socket exits if LISTEN_FDS is not set."""
        environ = {"LISTEN_FDS": "0"}
        with (
            patch.dict("os.environ", environ, clear=False),
            patch("sys.exit", side_effect=SystemExit(1)),
            pytest.raises(SystemExit),
        ):
            _build_server_socket(frozenset())

    def test_main_exits_on_missing_listen_fds(self):
        """_build_server_socket exits if LISTEN_FDS is missing."""
        environ = {}
        with (
            patch.dict("os.environ", environ, clear=True),
            patch("sys.exit", side_effect=SystemExit(1)),
            pytest.raises(SystemExit),
        ):
            _build_server_socket(frozenset())

    def test_main_logs_allowed_services(self):
        """_build_server_socket logs the set of allowed services."""
        environ = {"LISTEN_FDS": "1"}
        allowed = frozenset({"api.service", "web.service"})

        with (
            patch.dict("os.environ", environ, clear=False),
            patch("socket.fromfd") as mock_fromfd,
            patch("fraisier.systemctl_helper.logger") as mock_logger,
        ):
            mock_sock = MagicMock()
            mock_fromfd.return_value = mock_sock

            _build_server_socket(allowed)

            mock_logger.info.assert_called_once()
            call_args = mock_logger.info.call_args[0]
            assert "ready" in call_args[0]


class TestAllowlistIntegration:
    """Verify that only exactly matching service names are permitted."""

    def _call(self, request: dict, allowed: frozenset[str]) -> dict:
        import socket as _socket

        server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.sendall(json.dumps(request).encode() + b"\n")
        client.shutdown(_socket.SHUT_WR)
        _handle_connection(server, allowed)
        with client.makefile("rb") as f:
            raw = f.readline()
        client.close()
        return json.loads(raw.decode()) if raw else {}

    def test_partial_match_rejected(self):
        """'api' does not match 'api.service' — prefix match must be exact."""
        result = self._call(
            {"action": "stop", "service": "api"},
            frozenset({"api.service"}),
        )
        assert result["ok"] is False

    def test_suffix_injection_rejected(self):
        """'api.service; rm -rf /' must be rejected."""
        result = self._call(
            {"action": "stop", "service": "api.service; rm -rf /"},
            frozenset({"api.service"}),
        )
        assert result["ok"] is False

    def test_exact_match_accepted(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            result = self._call(
                {"action": "is-active", "service": "api.dev.example.com.service"},
                frozenset({"api.dev.example.com.service"}),
            )
        assert result["ok"] is True

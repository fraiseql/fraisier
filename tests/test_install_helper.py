"""Tests for fraisier.install_helper — socket-activated install helper."""

from __future__ import annotations

import json
import socket as _socket
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from fraisier.install_helper import (
    _handle_connection,
    _send_error,
    _send_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_socket_pair() -> tuple:
    server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    return server, client


def _recv_json(sock) -> dict:
    with sock.makefile("rb") as f:
        raw = f.readline()
    return json.loads(raw.decode())


def _call(request: dict) -> dict:
    """Send *request* via socket pair, call handler, return parsed response."""
    server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    client.sendall(json.dumps(request).encode() + b"\n")
    client.shutdown(_socket.SHUT_WR)
    _handle_connection(server)
    with client.makefile("rb") as f:
        raw = f.readline()
    client.close()
    return json.loads(raw.decode()) if raw else {}


# ---------------------------------------------------------------------------
# _send_response / _send_error
# ---------------------------------------------------------------------------


class TestSendResponse:
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

    def test_send_response_swallows_oserror(self):
        server, client = _make_socket_pair()
        client.close()
        # Should not raise
        _send_response(server, {"ok": True})
        server.close()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_rejects_missing_command(self):
        result = _call({"cwd": "/var/www/app"})
        assert result["ok"] is False
        assert "command" in result["error"]

    def test_rejects_empty_command(self):
        result = _call({"command": [], "cwd": "/var/www/app"})
        assert result["ok"] is False
        assert "command" in result["error"]

    def test_rejects_non_list_command(self):
        result = _call({"command": "uv sync", "cwd": "/var/www/app"})
        assert result["ok"] is False
        assert "command" in result["error"]

    def test_rejects_command_with_non_string_elements(self):
        result = _call({"command": ["uv", 42], "cwd": "/var/www/app"})
        assert result["ok"] is False
        assert "command" in result["error"]

    def test_rejects_missing_cwd(self):
        result = _call({"command": ["uv", "sync"]})
        assert result["ok"] is False
        assert "cwd" in result["error"]

    def test_rejects_relative_cwd(self):
        result = _call({"command": ["uv", "sync"], "cwd": "relative/path"})
        assert result["ok"] is False
        assert "cwd" in result["error"]

    def test_rejects_malformed_json(self):
        server, client = _make_socket_pair()
        client.sendall(b"not valid json\n")
        client.shutdown(_socket.SHUT_WR)
        # Should not raise
        _handle_connection(server)
        client.close()

    def test_empty_connection_is_handled_gracefully(self):
        server, client = _make_socket_pair()
        client.shutdown(_socket.SHUT_WR)
        # Should not raise
        _handle_connection(server)
        client.close()


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


class TestCommandExecution:
    def test_runs_command_in_cwd(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Resolved 5 packages"
        mock_result.stderr = ""

        req = {"command": ["uv", "sync", "--frozen"], "cwd": "/var/www/app"}
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = _call(req)

        mock_run.assert_called_once_with(
            ["uv", "sync", "--frozen"],
            cwd="/var/www/app",
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert result["ok"] is True
        assert result["returncode"] == 0
        assert result["stdout"] == "Resolved 5 packages"

    def test_command_failure_returns_ok_false(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error: lockfile out of date"

        req = {"command": ["uv", "sync", "--frozen"], "cwd": "/var/www/app"}
        with patch("subprocess.run", return_value=mock_result):
            result = _call(req)

        assert result["ok"] is False
        assert result["returncode"] == 1
        assert "lockfile out of date" in result["stderr"]

    def test_timeout_returns_ok_false(self):
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=[], timeout=600),
        ):
            result = _call({"command": ["uv", "sync"], "cwd": "/var/www/app"})

        assert result["ok"] is False
        assert "timed out" in result["error"]

    def test_oserror_returns_ok_false(self):
        with patch("subprocess.run", side_effect=OSError("command not found")):
            result = _call({"command": ["uv", "sync"], "cwd": "/var/www/app"})

        assert result["ok"] is False
        assert "failed to run command" in result["error"]

    def test_recv_oserror_handled_gracefully(self):
        mock_sock = MagicMock()
        mock_sock.recv.side_effect = OSError("network error")
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        # Should not raise
        _handle_connection(mock_sock)


# ---------------------------------------------------------------------------
# main() — LISTEN_FDS guard
# ---------------------------------------------------------------------------


class TestMain:
    def test_exits_when_listen_fds_not_set(self):
        with (
            patch.dict("os.environ", {"LISTEN_FDS": "0"}, clear=False),
            patch("sys.exit", side_effect=SystemExit(1)),
            pytest.raises(SystemExit),
        ):
            from fraisier.install_helper import main

            main()

    def test_exits_when_listen_fds_missing(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("sys.exit", side_effect=SystemExit(1)),
            pytest.raises(SystemExit),
        ):
            from fraisier.install_helper import main

            main()

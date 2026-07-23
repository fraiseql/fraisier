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
    _serve_connection,
)

_DEFAULT_ALLOWED = ["uv", "sync", "--frozen"]

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


def _call(
    request: dict,
    allowed_command: list[str] | None = None,
) -> dict:
    """Send *request* via socket pair, call handler, return parsed response."""
    server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    client.sendall(json.dumps(request).encode() + b"\n")
    client.shutdown(_socket.SHUT_WR)
    _handle_connection(server, allowed_command=allowed_command or _DEFAULT_ALLOWED)
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
        _handle_connection(server, allowed_command=_DEFAULT_ALLOWED)
        client.close()

    def test_empty_connection_is_handled_gracefully(self):
        server, client = _make_socket_pair()
        client.shutdown(_socket.SHUT_WR)
        # Should not raise
        _handle_connection(server, allowed_command=_DEFAULT_ALLOWED)
        client.close()

    def test_rejects_command_not_in_allowlist(self):
        """Command that differs from the baked-in allowed command is rejected."""
        result = _call(
            {"command": ["rm", "-rf", "/"], "cwd": "/var/www/app"},
            allowed_command=_DEFAULT_ALLOWED,
        )
        assert result["ok"] is False
        assert "command not allowed" in result["error"]

    def test_rejection_names_expected_and_received(self):
        """The rejection names both commands and points at the stale allowlist (#279)."""
        result = _call(
            {"command": ["bash", "-c", "echo hi"], "cwd": "/var/www/app"},
            allowed_command=_DEFAULT_ALLOWED,
        )
        assert result["ok"] is False
        # Both the received and the allowed command appear in the error.
        assert "bash" in result["error"]
        for token in _DEFAULT_ALLOWED:
            assert token in result["error"]
        # Advice points at the re-bake path + issue.
        assert "advice" in result
        assert "install.command" in result["advice"]
        assert "279" in result["advice"]

    def test_accepts_exact_allowed_command(self):
        """Exact match of allowed command passes the allowlist check."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            result = _call(
                {"command": _DEFAULT_ALLOWED, "cwd": "/var/www/app"},
                allowed_command=_DEFAULT_ALLOWED,
            )
        assert result["ok"] is True


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
            result = _call(
                {"command": ["uv", "sync"], "cwd": "/var/www/app"},
                allowed_command=["uv", "sync"],
            )

        assert result["ok"] is False
        assert "timed out" in result["error"]

    def test_oserror_returns_ok_false(self):
        with patch("subprocess.run", side_effect=OSError("command not found")):
            result = _call(
                {"command": ["uv", "sync"], "cwd": "/var/www/app"},
                allowed_command=["uv", "sync"],
            )

        assert result["ok"] is False
        assert "failed to run command" in result["error"]

    def test_recv_oserror_handled_gracefully(self):
        mock_sock = MagicMock()
        mock_sock.recv.side_effect = OSError("network error")
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        # Should not raise
        _handle_connection(mock_sock, allowed_command=_DEFAULT_ALLOWED)


# ---------------------------------------------------------------------------
# main() — LISTEN_FDS guard
# ---------------------------------------------------------------------------


class TestMain:
    def test_exits_when_listen_fds_not_set(self):
        with (
            patch("sys.argv", ["fraisier-install-helper", "uv", "sync", "--frozen"]),
            patch.dict("os.environ", {"LISTEN_FDS": "0"}, clear=False),
            patch("sys.exit", side_effect=SystemExit(1)),
            pytest.raises(SystemExit),
        ):
            from fraisier.install_helper import main

            main()

    def test_exits_when_listen_fds_missing(self):
        with (
            patch("sys.argv", ["fraisier-install-helper", "uv", "sync", "--frozen"]),
            patch.dict("os.environ", {}, clear=True),
            patch("sys.exit", side_effect=SystemExit(1)),
            pytest.raises(SystemExit),
        ):
            from fraisier.install_helper import main

            main()

    def test_exits_when_no_allowed_command(self):
        with (
            patch("sys.argv", ["fraisier-install-helper"]),
            patch.dict("os.environ", {"LISTEN_FDS": "1"}, clear=False),
            patch("sys.exit", side_effect=SystemExit(1)),
            pytest.raises(SystemExit),
        ):
            from fraisier.install_helper import main

            main()


class TestMainConnectionErrorLogging:
    """The main() loop must catch handler crashes and log them with the
    exception object bound to a name and passed as a format arg, so the
    type/repr appears in the rendered log line.
    """

    def test_handler_crash_is_logged_with_exception_object(self):
        from fraisier import install_helper

        boom = RuntimeError("simulated install handler crash")
        fake_conn = MagicMock()
        fake_sock = MagicMock()
        fake_sock.accept.side_effect = [
            (fake_conn, "/tmp/x"),
            OSError("loop exit"),
        ]

        with (
            patch.dict("os.environ", {"LISTEN_FDS": "1"}, clear=False),
            patch.object(install_helper.socket, "fromfd", return_value=fake_sock),
            patch.object(install_helper, "_handle_connection", side_effect=boom),
            patch.object(install_helper, "logger") as mock_logger,
        ):
            install_helper.main()

        mock_logger.exception.assert_called_once()
        call_args = mock_logger.exception.call_args
        assert boom in call_args.args, (
            f"Expected {boom!r} in logger.exception args, got {call_args.args!r}"
        )


# ---------------------------------------------------------------------------
# Phase 3 cycle 3.1 — SO_PEERCRED retrofit
# ---------------------------------------------------------------------------


class TestServeConnectionEnforcesPeerCreds:
    """``_serve_connection`` runs ``check_peer_creds`` before dispatching."""

    def test_rejects_non_matching_uid(self):
        import os

        server, client = _make_socket_pair()
        wrong_uid = os.getuid() + 1
        _serve_connection(
            server,
            expected_uid=wrong_uid,
            allowed_command=["uv", "sync", "--frozen"],
        )
        data = _recv_json(client)
        client.close()
        assert data["ok"] is False
        assert "peer" in data["error"].lower()

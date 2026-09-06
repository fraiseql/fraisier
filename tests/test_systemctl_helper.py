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
    _serve_connection,
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

    def test_contains_enable(self):
        """#382: the scheduled deployer enables its timer through the helper,
        because both deploy-hosting units set NoNewPrivileges and sudo cannot
        run under them."""
        assert "enable" in _ALLOWED_ACTIONS

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
        result = self._call(
            {"action": "disable", "service": "foo.service"}, frozenset()
        )
        assert result["ok"] is False
        assert "action not allowed" in result["error"]

    def test_enable_allowed_service_calls_systemctl(self):
        """#382: enable reaches systemctl for an allowlisted timer."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = self._call(
                {"action": "enable", "service": "backup.timer"},
                frozenset({"backup.timer"}),
            )

        mock_run.assert_called_once()
        assert mock_run.call_args[0][0] == [
            "/usr/bin/systemctl",
            "enable",
            "backup.timer",
        ]
        assert result["ok"] is True

    def test_enable_rejected_for_service_not_in_allowlist(self):
        """#382: enable is gated by the same service allowlist as stop/start."""
        result = self._call(
            {"action": "enable", "service": "evil.timer"},
            frozenset({"backup.timer"}),
        )
        assert result["ok"] is False
        assert "service not allowed" in result["error"]

    def test_enable_rejects_missing_service_field(self):
        """#382: enable is not a service-less action like daemon-reload."""
        result = self._call({"action": "enable"}, frozenset({"backup.timer"}))
        assert result["ok"] is False
        assert "missing 'service'" in result["error"]

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

    def test_restart_allowed_for_webhook_unit(self):
        """Contract for #162: when the webhook unit is in the allowlist, the
        helper accepts restart for it. Locks the wire-up that the self-upgrade
        runner depends on."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""

        webhook = "fraisier-myproj-webhook.service"
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = self._call(
                {"action": "restart", "service": webhook},
                frozenset({webhook}),
            )

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["/usr/bin/systemctl", "restart", webhook]
        assert result["ok"] is True

    def test_restart_rejected_for_webhook_when_not_in_allowlist(self):
        """Regression guard: without #162's allowlist addition, a restart RPC
        for the webhook unit must be rejected (not silently allowed)."""
        result = self._call(
            {"action": "restart", "service": "fraisier-myproj-webhook.service"},
            frozenset({"some_other.service"}),
        )
        assert result["ok"] is False
        assert "service not allowed" in result["error"]

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


class TestIsActiveExitCodes:
    """Helper returns ok=False + returncode=3 for inactive services (issue #183)."""

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

    def test_is_active_inactive_returns_exit_3(self):
        """systemctl is-active returns exit code 3 for an inactive service."""
        mock_result = MagicMock()
        mock_result.returncode = 3
        mock_result.stdout = "inactive\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            result = self._call(
                {"action": "is-active", "service": "api.service"},
                frozenset({"api.service"}),
            )

        assert result["ok"] is False
        assert result["returncode"] == 3
        assert result["stdout"] == "inactive\n"

    def test_is_active_active_returns_exit_0(self):
        """systemctl is-active returns exit code 0 for an active service."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "active\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result):
            result = self._call(
                {"action": "is-active", "service": "api.service"},
                frozenset({"api.service"}),
            )

        assert result["ok"] is True
        assert result["returncode"] == 0
        assert result["stdout"] == "active\n"


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


class TestMainConnectionErrorLogging:
    """The main() loop must catch handler crashes and log them with the
    exception object bound, not silently swallow them. logger.exception
    captures the traceback automatically, but the exception must also be
    bound to a name and embedded in the message so the type/repr appears
    in the formatted log line (grep-friendly post-mortem).
    """

    def test_handler_crash_is_logged_with_exception_object(self):
        """A crashing handler is logged, with the exception object in the
        rendered line, and the loop keeps serving.

        The accept loop itself lives in ``helper_version`` since #391, so
        that is where the log line comes from; this still exercises the
        wiring end to end through ``main()``.
        """
        from fraisier import helper_version, systemctl_helper

        boom = RuntimeError("simulated handler crash")
        fake_conn = MagicMock()
        fake_sock = MagicMock()
        # First accept() returns a connection; second raises OSError to break
        # the while-True loop cleanly.
        fake_sock.accept.side_effect = [
            (fake_conn, "/tmp/x"),
            OSError("loop exit"),
        ]

        with (
            patch.object(
                systemctl_helper, "_build_server_socket", return_value=fake_sock
            ),
            patch.object(systemctl_helper, "_handle_connection", side_effect=boom),
            patch.object(helper_version, "logger") as mock_logger,
            patch.object(systemctl_helper.sys, "argv", ["fraisier-systemctl-helper"]),
        ):
            systemctl_helper.main()

        mock_logger.exception.assert_called_once()
        call_args = mock_logger.exception.call_args
        # The exception object must be passed as a format argument so it
        # appears in the rendered message line.
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
            allowed_services=frozenset({"foo.service"}),
        )
        data = _recv_json(client)
        client.close()
        assert data["ok"] is False
        assert "peer" in data["error"].lower()

    def test_none_expected_uid_skips_check(self):
        """``expected_uid=None`` is the transitional fallback for old units."""
        import os

        server, client = _make_socket_pair()
        # Send a daemon-reload request so _handle_connection has work.
        client.sendall(b'{"action": "daemon-reload"}\n')
        client.shutdown(_socket.SHUT_WR)
        with patch("fraisier.systemctl_helper.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            _serve_connection(
                server,
                expected_uid=None,
                allowed_services=frozenset(),
            )
        # The handler ran (dispatched daemon-reload); peer-creds was bypassed.
        run.assert_called_once()
        del os, client

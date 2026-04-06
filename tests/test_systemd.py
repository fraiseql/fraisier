"""Tests for SystemdServiceManager."""

import json
import socket
import subprocess
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

from fraisier.systemd import SystemdServiceManager, _call_via_socket

# ---------------------------------------------------------------------------
# _call_via_socket
# ---------------------------------------------------------------------------


class TestCallViaSocket:
    """_call_via_socket serializes/deserializes JSON over a Unix socket pair."""

    def _run_fake_server(self, server_sock: socket.socket, response: dict) -> None:
        """Accept one connection, read request, send *response*, close."""
        conn, _ = server_sock.accept()
        with conn.makefile("rb") as f:
            f.readline()  # consume request
        conn.sendall(json.dumps(response).encode() + b"\n")
        conn.close()

    def _make_socket_path(self, tmp_path) -> str:
        sock_path = str(tmp_path / "test.sock")
        return sock_path

    def test_sends_action_and_service(self, tmp_path):
        """Request JSON is correctly built and sent."""
        sock_path = self._make_socket_path(tmp_path)
        received: list[dict] = []

        def fake_server():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
                srv.bind(sock_path)
                srv.listen(1)
                conn, _ = srv.accept()
                with conn.makefile("rb") as f:
                    raw = f.readline()
                received.append(json.loads(raw.decode()))
                ok_resp = {"ok": True, "stdout": "", "stderr": "", "returncode": 0}
                conn.sendall(json.dumps(ok_resp).encode() + b"\n")
                conn.close()

        t = threading.Thread(target=fake_server)
        t.start()
        t.join(timeout=0)  # let thread start

        import time

        time.sleep(0.05)

        _call_via_socket(sock_path, "stop", "api.service")
        t.join(timeout=2)

        assert received[0] == {"action": "stop", "service": "api.service"}

    def test_daemon_reload_omits_service(self, tmp_path):
        """daemon-reload request has no 'service' key."""
        sock_path = self._make_socket_path(tmp_path)
        received: list[dict] = []

        def fake_server():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
                srv.bind(sock_path)
                srv.listen(1)
                conn, _ = srv.accept()
                with conn.makefile("rb") as f:
                    raw = f.readline()
                received.append(json.loads(raw.decode()))
                ok_resp = {"ok": True, "stdout": "", "stderr": "", "returncode": 0}
                conn.sendall(json.dumps(ok_resp).encode() + b"\n")
                conn.close()

        t = threading.Thread(target=fake_server)
        t.start()

        import time

        time.sleep(0.05)

        _call_via_socket(sock_path, "daemon-reload")
        t.join(timeout=2)

        assert received[0] == {"action": "daemon-reload"}
        assert "service" not in received[0]

    def test_returns_simplenamespace_with_expected_attrs(self, tmp_path):
        """Return value has .stdout, .stderr, .returncode."""
        sock_path = self._make_socket_path(tmp_path)

        def fake_server():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
                srv.bind(sock_path)
                srv.listen(1)
                conn, _ = srv.accept()
                with conn.makefile("rb") as f:
                    f.readline()
                resp = {"ok": True, "stdout": "active\n", "stderr": "", "returncode": 0}
                conn.sendall(json.dumps(resp).encode() + b"\n")
                conn.close()

        t = threading.Thread(target=fake_server)
        t.start()

        import time

        time.sleep(0.05)

        result = _call_via_socket(sock_path, "is-active", "api.service")
        t.join(timeout=2)

        assert isinstance(result, types.SimpleNamespace)
        assert result.stdout == "active\n"
        assert result.stderr == ""
        assert result.returncode == 0

    def test_ok_false_raises_called_process_error(self, tmp_path):
        """ok=false response raises CalledProcessError."""
        sock_path = self._make_socket_path(tmp_path)

        def fake_server():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
                srv.bind(sock_path)
                srv.listen(1)
                conn, _ = srv.accept()
                with conn.makefile("rb") as f:
                    f.readline()
                conn.sendall(
                    json.dumps(
                        {"ok": False, "error": "service not allowed: evil.service"}
                    ).encode()
                    + b"\n"
                )
                conn.close()

        t = threading.Thread(target=fake_server)
        t.start()

        import time

        time.sleep(0.05)

        with pytest.raises(subprocess.CalledProcessError):
            _call_via_socket(sock_path, "stop", "evil.service")
        t.join(timeout=2)

    def test_missing_socket_raises_connection_refused(self, tmp_path):
        """If socket file does not exist, ConnectionRefusedError is raised."""
        sock_path = str(tmp_path / "nonexistent.sock")
        with pytest.raises(ConnectionRefusedError):
            _call_via_socket(sock_path, "stop", "api.service")


# ---------------------------------------------------------------------------
# SystemdServiceManager: socket path takes precedence
# ---------------------------------------------------------------------------


class TestSystemdManagerSocketPath:
    """Socket path is checked before wrapper and sudo."""

    def test_restart_uses_socket_when_set(self, monkeypatch, tmp_path):
        """When FRAISIER_SYSTEMCTL_SOCKET is set, _call_via_socket is used."""
        sock = "/run/fraisier/systemctl-test.sock"
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", sock)
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_WRAPPER", raising=False)

        runner = MagicMock()
        mgr = SystemdServiceManager(runner)

        with patch("fraisier.systemd._call_via_socket") as mock_socket:
            mock_socket.return_value = types.SimpleNamespace(
                stdout="", stderr="", returncode=0
            )
            mgr.restart("api.service")

        mock_socket.assert_called_once_with(sock, "restart", "api.service")
        runner.run.assert_not_called()

    def test_status_uses_socket_when_set(self, monkeypatch):
        """status() routes through socket when FRAISIER_SYSTEMCTL_SOCKET is set."""
        sock = "/run/fraisier/systemctl-test.sock"
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", sock)
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_WRAPPER", raising=False)

        runner = MagicMock()
        mgr = SystemdServiceManager(runner)

        with patch("fraisier.systemd._call_via_socket") as mock_socket:
            mock_socket.return_value = types.SimpleNamespace(
                stdout="active\n", stderr="", returncode=0
            )
            result = mgr.status("api.service")

        assert result == "active"
        mock_socket.assert_called_once_with(sock, "is-active", "api.service")
        runner.run.assert_not_called()

    def test_socket_takes_precedence_over_wrapper(self, monkeypatch):
        """Socket path wins over wrapper even when both env vars are set."""
        monkeypatch.setenv(
            "FRAISIER_SYSTEMCTL_SOCKET", "/run/fraisier/systemctl-test.sock"
        )
        monkeypatch.setenv(
            "FRAISIER_SYSTEMCTL_WRAPPER",
            "/usr/local/libexec/fraisier/systemctl-test",
        )

        runner = MagicMock()
        mgr = SystemdServiceManager(runner)

        with patch("fraisier.systemd._call_via_socket") as mock_socket:
            mock_socket.return_value = types.SimpleNamespace(
                stdout="", stderr="", returncode=0
            )
            mgr.restart("api.service")

        mock_socket.assert_called_once()
        runner.run.assert_not_called()

    def test_daemon_reload_uses_socket_when_set(self, monkeypatch):
        """daemon_reload() uses socket when FRAISIER_SYSTEMCTL_SOCKET is set."""
        sock = "/run/fraisier/systemctl-test.sock"
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", sock)
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_WRAPPER", raising=False)

        runner = MagicMock()
        mgr = SystemdServiceManager(runner)

        with patch("fraisier.systemd._call_via_socket") as mock_socket:
            mock_socket.return_value = types.SimpleNamespace(
                stdout="", stderr="", returncode=0
            )
            mgr.daemon_reload()

        mock_socket.assert_called_once_with(sock, "daemon-reload")
        runner.run.assert_not_called()

    def test_daemon_reload_falls_back_to_sudo(self, monkeypatch):
        """daemon_reload() falls back to sudo when no socket or wrapper is set."""
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_SOCKET", raising=False)
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_WRAPPER", raising=False)

        runner = MagicMock()
        runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mgr = SystemdServiceManager(runner)

        mgr.daemon_reload()

        runner.run.assert_called_once_with(
            ["sudo", "systemctl", "daemon-reload"],
            timeout=60,
            check=True,
        )


class TestRestart:
    """SystemdServiceManager.restart() calls systemctl restart via runner."""

    def test_restart_calls_systemctl_restart(self, monkeypatch):
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_SOCKET", raising=False)
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_WRAPPER", raising=False)
        runner = MagicMock()
        runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mgr = SystemdServiceManager(runner)

        mgr.restart("myapi")

        runner.run.assert_called_once_with(
            ["sudo", "systemctl", "restart", "myapi"],
            timeout=60,
            check=True,
        )

    def test_restart_with_custom_timeout(self, monkeypatch):
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_SOCKET", raising=False)
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_WRAPPER", raising=False)
        runner = MagicMock()
        runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mgr = SystemdServiceManager(runner)

        mgr.restart("myapi", timeout=120)

        runner.run.assert_called_once_with(
            ["sudo", "systemctl", "restart", "myapi"],
            timeout=120,
            check=True,
        )

    def test_restart_invalid_name_raises_valueerror(self):
        runner = MagicMock()
        mgr = SystemdServiceManager(runner)

        with pytest.raises(ValueError, match="Invalid service name"):
            mgr.restart("my;service")

        runner.run.assert_not_called()

    def test_restart_propagates_subprocess_error(self):
        runner = MagicMock()
        runner.run.side_effect = subprocess.CalledProcessError(1, "systemctl")
        mgr = SystemdServiceManager(runner)

        with pytest.raises(subprocess.CalledProcessError):
            mgr.restart("myapi")


class TestStatus:
    """SystemdServiceManager.status() returns parsed systemctl output."""

    def test_status_returns_active_state(self, monkeypatch):
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_SOCKET", raising=False)
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_WRAPPER", raising=False)
        runner = MagicMock()
        runner.run.return_value = MagicMock(returncode=0, stdout="active\n", stderr="")
        mgr = SystemdServiceManager(runner)

        result = mgr.status("myapi")

        assert result == "active"
        runner.run.assert_called_once_with(
            ["sudo", "systemctl", "is-active", "myapi"],
            timeout=30,
            check=False,
        )

    def test_status_returns_inactive_state(self):
        runner = MagicMock()
        runner.run.return_value = MagicMock(
            returncode=3, stdout="inactive\n", stderr=""
        )
        mgr = SystemdServiceManager(runner)

        result = mgr.status("myapi")

        assert result == "inactive"

    def test_status_invalid_name_raises_valueerror(self):
        runner = MagicMock()
        mgr = SystemdServiceManager(runner)

        with pytest.raises(ValueError, match="Invalid service name"):
            mgr.status("bad|name")

        runner.run.assert_not_called()

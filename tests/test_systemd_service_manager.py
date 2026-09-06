"""Tests for SystemdServiceManager."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from fraisier.service_managers.systemd import SystemdServiceManager, _call_via_socket


class TestSystemdServiceManager:
    """Test SystemdServiceManager implementation."""

    @pytest.fixture
    def mock_runner(self):
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_runner):
        return SystemdServiceManager(mock_runner)

    def test_start_service(self, manager, mock_runner):
        manager.start("test_service")
        mock_runner.run.assert_called_once_with(
            ["sudo", "systemctl", "start", "test_service"], timeout=60, check=True
        )

    def test_stop_service(self, manager, mock_runner):
        manager.stop("test_service")
        mock_runner.run.assert_called_once_with(
            ["sudo", "systemctl", "stop", "test_service"], timeout=60, check=True
        )

    def test_restart_service(self, manager, mock_runner):
        manager.restart("test_service")
        mock_runner.run.assert_called_once_with(
            ["sudo", "systemctl", "restart", "test_service"], timeout=60, check=True
        )

    def test_is_active_service_active(self, manager, mock_runner):
        mock_runner.run.return_value.stdout = "active\n"
        assert manager.is_active("test_service") is True
        mock_runner.run.assert_called_once_with(
            ["sudo", "systemctl", "is-active", "test_service"], timeout=30, check=False
        )

    def test_is_active_service_inactive(self, manager, mock_runner):
        mock_runner.run.return_value.stdout = "inactive\n"
        assert manager.is_active("test_service") is False

    def test_daemon_reload(self, manager, mock_runner):
        manager.daemon_reload()
        mock_runner.run.assert_called_once_with(
            ["sudo", "systemctl", "daemon-reload"], timeout=60, check=True
        )

    def test_invalid_service_name(self, manager):
        with pytest.raises(ValueError, match="Invalid service name"):
            manager.start("invalid service name")


# ---------------------------------------------------------------------------
# _call_via_socket: check parameter
# ---------------------------------------------------------------------------


def _make_socket_response(ok: bool, returncode: int, stdout: str) -> bytes:
    """Build a JSON response line as the systemctl helper would send it."""
    resp: dict = {"ok": ok, "stdout": stdout, "stderr": "", "returncode": returncode}
    if not ok:
        resp["error"] = f"Command returned exit status {returncode}"
    return json.dumps(resp).encode() + b"\n"


class TestCallViaSocketCheck:
    """_call_via_socket respects the check parameter."""

    def _patch_socket(self, response_bytes: bytes):
        """Return a context manager that patches socket.connect/sendall/makefile."""
        mock_sock = MagicMock()
        mock_file = MagicMock()
        mock_file.readline.return_value = response_bytes
        mock_sock.makefile.return_value.__enter__ = MagicMock(return_value=mock_file)
        mock_sock.makefile.return_value.__exit__ = MagicMock(return_value=False)
        return patch(
            "socket.socket",
            return_value=MagicMock(
                __enter__=MagicMock(return_value=mock_sock),
                __exit__=MagicMock(return_value=False),
            ),
        )

    def test_raises_on_nonzero_when_check_true(self):
        response = _make_socket_response(ok=False, returncode=3, stdout="inactive\n")
        with self._patch_socket(response):
            with pytest.raises(subprocess.CalledProcessError) as exc_info:
                _call_via_socket(
                    "/tmp/test.sock", "is-active", "test.service", check=True
                )
            assert exc_info.value.returncode == 3

    def test_returns_result_when_check_false(self):
        response = _make_socket_response(ok=False, returncode=3, stdout="inactive\n")
        with self._patch_socket(response):
            result = _call_via_socket(
                "/tmp/test.sock", "is-active", "test.service", check=False
            )
        assert result.stdout == "inactive\n"
        assert result.returncode == 3

    def test_returns_result_on_success(self):
        response = _make_socket_response(ok=True, returncode=0, stdout="active\n")
        with self._patch_socket(response):
            result = _call_via_socket(
                "/tmp/test.sock", "is-active", "test.service", check=False
            )
        assert result.stdout == "active\n"
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# SystemdServiceManager via socket path (integration with _call_via_socket)
# ---------------------------------------------------------------------------


class TestSystemdViaSocket:
    """SystemdServiceManager correctly handles is-active via socket path."""

    def _make_manager(self):
        return SystemdServiceManager(MagicMock())

    def _patch_socket_env(self, response_bytes: bytes):
        """Patch both FRAISIER_SYSTEMCTL_SOCKET env var and the socket layer."""
        mock_sock = MagicMock()
        mock_file = MagicMock()
        mock_file.readline.return_value = response_bytes
        mock_sock.makefile.return_value.__enter__ = MagicMock(return_value=mock_file)
        mock_sock.makefile.return_value.__exit__ = MagicMock(return_value=False)
        sock_patch = patch(
            "socket.socket",
            return_value=MagicMock(
                __enter__=MagicMock(return_value=mock_sock),
                __exit__=MagicMock(return_value=False),
            ),
        )
        env_patch = patch.dict(
            "os.environ", {"FRAISIER_SYSTEMCTL_SOCKET": "/tmp/test.sock"}
        )
        return env_patch, sock_patch

    def test_is_active_returns_false_when_inactive_via_socket(self):
        response = _make_socket_response(ok=False, returncode=3, stdout="inactive\n")
        env_patch, sock_patch = self._patch_socket_env(response)
        manager = self._make_manager()
        with env_patch, sock_patch:
            assert manager.is_active("test_service") is False

    def test_is_active_returns_true_when_active_via_socket(self):
        response = _make_socket_response(ok=True, returncode=0, stdout="active\n")
        env_patch, sock_patch = self._patch_socket_env(response)
        manager = self._make_manager()
        with env_patch, sock_patch:
            assert manager.is_active("test_service") is True

    def test_status_returns_inactive_on_exit_code_3_via_socket(self):
        response = _make_socket_response(ok=False, returncode=3, stdout="inactive\n")
        env_patch, sock_patch = self._patch_socket_env(response)
        manager = self._make_manager()
        with env_patch, sock_patch:
            assert manager.status("test_service") == "inactive"

    def test_stop_still_raises_via_socket(self):
        response = _make_socket_response(ok=False, returncode=1, stdout="")
        env_patch, sock_patch = self._patch_socket_env(response)
        manager = self._make_manager()
        with env_patch, sock_patch, pytest.raises(subprocess.CalledProcessError):
            manager.stop("test_service")


# ---------------------------------------------------------------------------
# enable (#382)
# ---------------------------------------------------------------------------


class TestSystemdEnable:
    """`enable` resolves through the same three transports as every other action.

    #382: the scheduled deployer used to spawn ``sudo systemctl enable`` itself,
    which cannot work from a ``NoNewPrivileges`` unit. Routing it through the
    manager means the socket wins whenever the host provides one.
    """

    @pytest.fixture
    def mock_runner(self):
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_runner):
        return SystemdServiceManager(mock_runner)

    def test_enable_falls_back_to_sudo(self, manager, mock_runner):
        with patch.dict("os.environ", {}, clear=True):
            manager.enable("backup.timer")
        mock_runner.run.assert_called_once_with(
            ["sudo", "systemctl", "enable", "backup.timer"], timeout=60, check=True
        )

    def test_enable_uses_wrapper_when_set(self, manager, mock_runner):
        with patch.dict(
            "os.environ", {"FRAISIER_SYSTEMCTL_WRAPPER": "/usr/local/bin/w"}, clear=True
        ):
            manager.enable("backup.timer")
        mock_runner.run.assert_called_once_with(
            ["/usr/local/bin/w", "enable", "backup.timer"], timeout=60, check=True
        )

    def test_enable_goes_through_the_socket_when_set(self, manager, mock_runner):
        response = _make_socket_response(ok=True, returncode=0, stdout="")
        mock_sock = MagicMock()
        mock_file = MagicMock()
        mock_file.readline.return_value = response
        mock_sock.makefile.return_value.__enter__ = MagicMock(return_value=mock_file)
        mock_sock.makefile.return_value.__exit__ = MagicMock(return_value=False)
        with (
            patch.dict("os.environ", {"FRAISIER_SYSTEMCTL_SOCKET": "/tmp/test.sock"}),
            patch(
                "socket.socket",
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=mock_sock),
                    __exit__=MagicMock(return_value=False),
                ),
            ),
        ):
            manager.enable("backup.timer")

        mock_runner.run.assert_not_called()
        sent = json.loads(mock_sock.sendall.call_args[0][0].decode())
        assert sent == {"action": "enable", "service": "backup.timer"}

    def test_enable_raises_when_the_helper_refuses(self, manager):
        response = _make_socket_response(ok=False, returncode=1, stdout="")
        mock_sock = MagicMock()
        mock_file = MagicMock()
        mock_file.readline.return_value = response
        mock_sock.makefile.return_value.__enter__ = MagicMock(return_value=mock_file)
        mock_sock.makefile.return_value.__exit__ = MagicMock(return_value=False)
        with (
            patch.dict("os.environ", {"FRAISIER_SYSTEMCTL_SOCKET": "/tmp/test.sock"}),
            patch(
                "socket.socket",
                return_value=MagicMock(
                    __enter__=MagicMock(return_value=mock_sock),
                    __exit__=MagicMock(return_value=False),
                ),
            ),
            pytest.raises(subprocess.CalledProcessError),
        ):
            manager.enable("backup.timer")

    def test_enable_validates_the_service_name(self, manager):
        with pytest.raises(ValueError, match="Invalid service name"):
            manager.enable("backup timer; rm -rf /")

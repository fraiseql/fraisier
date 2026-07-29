"""Tests for __pycache__ permission error diagnostics (#196)."""

from __future__ import annotations

import json
import socket as _socket
import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.mixins import _install_failure_advice
from fraisier.errors import DeploymentError
from fraisier.install_helper import _handle_connection

_DEFAULT_ALLOWED = ["uv", "sync", "--frozen"]

_PYCACHE_STDERR = (
    "error: failed to remove directory "
    "'/var/www/api/.venv/lib/python3.13/site-packages/fraisier/__pycache__': "
    "Permission denied (os error 13)"
)


# ---------------------------------------------------------------------------
# Phase 3, Cycle 1: install_helper.py returns advice field
# ---------------------------------------------------------------------------


class TestInstallHelperAdvice:
    def _call(self, request: dict, allowed_command: list[str] | None = None) -> dict:
        server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.sendall(json.dumps(request).encode() + b"\n")
        client.shutdown(_socket.SHUT_WR)
        _handle_connection(server, allowed_command=allowed_command or _DEFAULT_ALLOWED)
        with client.makefile("rb") as f:
            raw = f.readline()
        client.close()
        return json.loads(raw.decode()) if raw else {}

    def test_advice_added_on_pycache_permission_error(self):
        """When stderr contains __pycache__ + Permission denied, response has advice."""
        mock_result = MagicMock()
        mock_result.returncode = 2
        mock_result.stdout = ""
        mock_result.stderr = _PYCACHE_STDERR

        with patch("subprocess.run", return_value=mock_result):
            response = self._call({"command": _DEFAULT_ALLOWED, "cwd": "/var/www/api"})

        assert response["ok"] is False
        assert "advice" in response
        assert "__pycache__" in response["advice"]
        assert "issue/196" in response["advice"] or "issues/196" in response["advice"]

    def test_no_advice_on_regular_failure(self):
        """Regular failures don't get an advice field."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error: lockfile out of date"

        with patch("subprocess.run", return_value=mock_result):
            response = self._call({"command": _DEFAULT_ALLOWED, "cwd": "/var/www/api"})

        assert response["ok"] is False
        assert "advice" not in response


# ---------------------------------------------------------------------------
# Phase 3, Cycle 2: _install_via_socket surfaces advice in DeploymentError
# ---------------------------------------------------------------------------


class TestInstallViaSocketAdvice:
    def test_advice_included_in_deployment_error(self, tmp_path):
        """When response has advice field, DeploymentError message includes it."""
        sock_path = str(tmp_path / "test.sock")
        server_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        server_sock.bind(sock_path)
        server_sock.listen(1)

        response_payload = (
            json.dumps(
                {
                    "ok": False,
                    "stdout": "",
                    "stderr": _PYCACHE_STDERR,
                    "returncode": 2,
                    "advice": "Root-owned __pycache__ directories are blocking uv sync.",
                }
            ).encode()
            + b"\n"
        )

        def _serve():
            conn, _ = server_sock.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(response_payload)
            server_sock.close()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()

        cfg = {"app_path": "/var/www/api", "fraise_name": "api", "environment": "prod"}
        deployer = APIDeployer(cfg)

        with pytest.raises(DeploymentError, match=r"Advice:.*__pycache__"):
            deployer._install_via_socket(
                sock_path, ["uv", "sync", "--frozen"], "/var/www/api"
            )
        t.join(timeout=2)


# ---------------------------------------------------------------------------
# Phase 3, Cycle 3: sudo fallback path includes advice
# ---------------------------------------------------------------------------


class TestSudoFallbackAdvice:
    def test_advice_on_pycache_permission_in_sudo_fallback(self):
        """CalledProcessError with __pycache__+Permission denied gets advice."""
        config = {
            "app_path": "/var/www/api",
            "deploy_user": "fraisier",
            "fraise_name": "api",
            "environment": "production",
            "install": {
                "command": ["uv", "sync", "--frozen"],
                "user": "appuser",
            },
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        exc = subprocess.CalledProcessError(
            2, ["sudo", "-u", "appuser", "uv", "sync", "--frozen"]
        )
        exc.stdout = ""
        exc.stderr = _PYCACHE_STDERR
        mock_runner.run.side_effect = exc
        deployer.runner = mock_runner

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "fraisier.deployers.mixins.shutil.which",
                return_value="/usr/local/bin/uv",
            ),
            pytest.raises(DeploymentError, match=r"Advice:.*__pycache__"),
        ):
            deployer._install_dependencies()

    def test_install_failure_message_includes_stderr(self):
        """Captured stderr is surfaced in the error message itself (#277).

        Before the fix the stderr lived only in ``context["stderr"]`` and was
        never rendered, so the deploy journal hid the real cause.
        """
        config = {
            "app_path": "/var/www/api",
            "deploy_user": "fraisier",
            "fraise_name": "api",
            "environment": "production",
            "install": {"command": ["uv", "sync", "--frozen"], "user": "appuser"},
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        exc = subprocess.CalledProcessError(1, ["sudo", "-H", "-u", "appuser", "uv"])
        exc.stdout = ""
        exc.stderr = "boom: something is denied"
        mock_runner.run.side_effect = exc
        deployer.runner = mock_runner

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "fraisier.deployers.mixins.shutil.which",
                return_value="/usr/local/bin/uv",
            ),
            pytest.raises(DeploymentError) as exc_info,
        ):
            deployer._install_dependencies()

        # In the message (args[0]), not only in context["stderr"].
        assert "boom: something is denied" in exc_info.value.args[0]
        assert exc_info.value.context["stderr"] == "boom: something is denied"

    def test_cache_permission_denied_advice(self):
        """.cache + Permission denied points at the HOME/#276 fix (#277)."""
        config = {
            "app_path": "/var/www/api",
            "deploy_user": "fraisier",
            "fraise_name": "api",
            "environment": "production",
            "install": {"command": ["uv", "sync", "--frozen"], "user": "appuser"},
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        exc = subprocess.CalledProcessError(1, ["sudo", "-H", "-u", "appuser", "uv"])
        exc.stdout = ""
        exc.stderr = "Failed to initialize cache at /root/.cache/uv: Permission denied"
        mock_runner.run.side_effect = exc
        deployer.runner = mock_runner

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "fraisier.deployers.mixins.shutil.which",
                return_value="/usr/local/bin/uv",
            ),
            pytest.raises(DeploymentError) as exc_info,
        ):
            deployer._install_dependencies()

        msg = str(exc_info.value)
        assert "Advice:" in msg
        assert "HOME" in msg
        assert "276" in msg

    def test_cache_advice_not_triggered_by_pycache(self):
        """__pycache__ stays the more specific match; .cache does not shadow it."""
        advice = _install_failure_advice(_PYCACHE_STDERR, app_path="/var/www/api")
        assert advice is not None
        assert "__pycache__" in advice
        assert "276" not in advice


# ---------------------------------------------------------------------------
# Phase 4: Integration test — socket returns advice, deployer surfaces it
# ---------------------------------------------------------------------------


class TestIntegrationPycacheAdvice:
    def test_deploy_with_pycache_error_gets_advice(self, tmp_path):
        """Full path: socket returns pycache error+advice, deployer raises with both."""
        sock_path = str(tmp_path / "install.sock")
        server_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        server_sock.bind(sock_path)
        server_sock.listen(1)

        advice_text = (
            "Root-owned __pycache__ directories are blocking uv sync. "
            "Fix: sudo find /var/www/api/.venv -name __pycache__ -user root "
            "-type d -exec rm -rf {} + then retry the deployment. "
            "The venv may be corrupted — run uv sync --frozen manually "
            "after cleanup. See: https://github.com/fraiseql/fraisier/issues/196"
        )
        response_payload = (
            json.dumps(
                {
                    "ok": False,
                    "stdout": "",
                    "stderr": _PYCACHE_STDERR,
                    "returncode": 2,
                    "advice": advice_text,
                }
            ).encode()
            + b"\n"
        )

        def _serve():
            conn, _ = server_sock.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(response_payload)
            server_sock.close()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()

        # Create the socket file so Path(socket_path).exists() passes
        config = {
            "app_path": "/var/www/api",
            "deploy_user": "fraisier",
            "fraise_name": "api",
            "environment": "production",
            "install": {
                "command": ["uv", "sync", "--frozen"],
                "user": "appuser",
            },
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        deployer.runner = mock_runner

        with (
            patch.dict(
                "os.environ",
                {"FRAISIER_INSTALL_SOCKET_API_PRODUCTION": sock_path},
            ),
            pytest.raises(DeploymentError) as exc_info,
        ):
            deployer._install_dependencies()

        t.join(timeout=2)

        error = exc_info.value
        assert _PYCACHE_STDERR in str(error)
        assert "Advice:" in str(error)
        assert "venv may be corrupted" in str(error)

    def test_socket_failure_cache_advice(self, tmp_path):
        """Socket path derives a cache-write advice — without the sudo -H hint.

        The socket helper already runs as the install user with a correct
        HOME, so a .cache denial there must not send the operator to
        ``sudo -H`` (the fallback-path fix, #276), which does not apply.
        """
        sock_path = str(tmp_path / "install.sock")
        server_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        server_sock.bind(sock_path)
        server_sock.listen(1)

        cache_stderr = (
            "error: Failed to initialize cache at /home/appuser/.cache/uv: "
            "Permission denied (os error 13)"
        )
        response_payload = (
            json.dumps(
                {
                    "ok": False,
                    "stdout": "",
                    "stderr": cache_stderr,
                    "returncode": 1,
                }
            ).encode()
            + b"\n"
        )

        def _serve():
            conn, _ = server_sock.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(response_payload)
            server_sock.close()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()

        config = {
            "app_path": "/var/www/api",
            "deploy_user": "fraisier",
            "fraise_name": "api",
            "environment": "production",
            "install": {"command": ["uv", "sync", "--frozen"], "user": "appuser"},
        }
        deployer = APIDeployer(config)
        deployer.runner = MagicMock()

        with (
            patch.dict(
                "os.environ", {"FRAISIER_INSTALL_SOCKET_API_PRODUCTION": sock_path}
            ),
            pytest.raises(DeploymentError) as exc_info,
        ):
            deployer._install_dependencies()

        t.join(timeout=2)

        msg = str(exc_info.value)
        assert "Advice:" in msg
        assert "cache" in msg
        # Socket path never shells out to sudo, so it must not suggest sudo -H.
        assert "sudo -H" not in msg
        assert "276" not in msg


class TestAdviceMatchesTheSweep:
    """The remediation must fix the failure it is shown for (#303).

    Both advice strings hardcoded ``-user root``. The residue that produces
    this exact ``Permission denied`` can be owned by ``service.user`` (#292) or
    ``install.user`` (#286) — neither is root, so the suggested command was a
    no-op for the cases most likely to hit it.
    """

    def test_deployer_advice_does_not_assume_root(self):
        from fraisier.deployers.mixins import _install_failure_advice

        advice = _install_failure_advice(
            "error: Permission denied ... __pycache__ ...", app_path="/var/www/api"
        )

        assert advice is not None
        assert "-user root" not in advice
        assert "! -user" in advice
        assert "stat -c %U" in advice

    def test_deployer_advice_still_names_the_venv(self):
        from fraisier.deployers.mixins import _install_failure_advice

        advice = _install_failure_advice(
            "error: Permission denied ... __pycache__ ...", app_path="/var/www/api"
        )

        assert advice is not None
        assert "/var/www/api/.venv" in advice

    def test_helper_advice_does_not_assume_root(self):
        """The socket helper's advice reaches the operator through the deployer."""
        import inspect

        from fraisier import install_helper

        source = inspect.getsource(install_helper._handle_connection)
        pycache_advice = source.split("__pycache__ directories are blocking", 1)[1]
        pycache_advice = pycache_advice.split("issues/196", 1)[0]

        assert "-user root" not in pycache_advice
        assert "! -user" in pycache_advice

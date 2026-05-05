"""Tests for fraisier.scaffold_install_helper — root-privileged scaffold helper."""

from __future__ import annotations

import json
import socket as _socket
from unittest.mock import MagicMock, patch

import pytest

from fraisier.scaffold_install_helper import (
    _build_server_socket,
    _handle_connection,
    _send_error,
    _send_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_socket_pair():
    """Return a connected (server_conn, client_conn) Unix socket pair."""
    server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    return server, client


def _recv_json(sock) -> dict:
    """Read one JSON line from *sock* and return the parsed object."""
    with sock.makefile("rb") as f:
        raw = f.readline()
    return json.loads(raw.decode())


def _call(request: dict, allowed_script: str) -> dict:
    """Send *request* via socket pair, call handler, return parsed response."""
    server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    client.sendall(json.dumps(request).encode() + b"\n")
    client.shutdown(_socket.SHUT_WR)
    _handle_connection(server, allowed_script=allowed_script)
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
        # Should not raise, just log warning
        _send_response(server, {"ok": True})
        server.close()


# ---------------------------------------------------------------------------
# _handle_connection
# ---------------------------------------------------------------------------


class TestHandleConnection:
    def test_valid_install_request_runs_script(self, tmp_path):
        """Valid {"action": "install"} runs the allowed script."""
        script = tmp_path / "install.sh"
        script.write_text("#!/bin/bash\necho ok")
        script.chmod(0o755)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "ok\n"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = _call({"action": "install"}, allowed_script=str(script))

        assert result["ok"] is True
        assert result["returncode"] == 0
        args = mock_run.call_args[0][0]
        assert args == ["/usr/bin/bash", str(script)]

    def test_unknown_action_is_rejected(self, tmp_path):
        """Unknown actions are rejected without running the script."""
        script = tmp_path / "install.sh"
        script.touch()
        result = _call({"action": "rm_rf"}, allowed_script=str(script))
        assert result["ok"] is False
        assert "action not allowed" in result["error"]

    def test_install_script_not_found_returns_error(self, tmp_path):
        """Missing script path returns ok=False with 'not found' message."""
        result = _call(
            {"action": "install"}, allowed_script=str(tmp_path / "nonexistent.sh")
        )
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_script_failure_returns_ok_false(self, tmp_path):
        """Non-zero exit from the script propagates as ok=False."""
        script = tmp_path / "install.sh"
        script.write_text("#!/bin/bash\nexit 1")
        script.chmod(0o755)

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error\n"

        with patch("subprocess.run", return_value=mock_result):
            result = _call({"action": "install"}, allowed_script=str(script))

        assert result["ok"] is False
        assert result["returncode"] == 1

    def test_malformed_json_is_handled_gracefully(self, tmp_path):
        script = tmp_path / "install.sh"
        script.touch()
        server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.sendall(b"not valid json\n")
        client.shutdown(_socket.SHUT_WR)
        _handle_connection(server, allowed_script=str(script))
        client.close()

    def test_empty_connection_is_handled_gracefully(self, tmp_path):
        script = tmp_path / "install.sh"
        script.touch()
        server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.shutdown(_socket.SHUT_WR)
        _handle_connection(server, allowed_script=str(script))
        client.close()

    def test_timeout_expired_sends_error(self, tmp_path):
        """TimeoutExpired is caught and returns an error response."""
        import subprocess

        script = tmp_path / "install.sh"
        script.write_text("#!/bin/bash\necho ok")
        script.chmod(0o755)

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=[], timeout=300),
        ):
            result = _call({"action": "install"}, allowed_script=str(script))

        assert result["ok"] is False
        assert "timed out" in result["error"]

    def test_oserror_from_subprocess_sends_error(self, tmp_path):
        """OSError from subprocess.run is caught and returns error response."""
        script = tmp_path / "install.sh"
        script.write_text("#!/bin/bash\necho ok")
        script.chmod(0o755)

        with patch("subprocess.run", side_effect=OSError("exec failed")):
            result = _call({"action": "install"}, allowed_script=str(script))

        assert result["ok"] is False
        assert "failed to run" in result["error"]


# ---------------------------------------------------------------------------
# _build_server_socket
# ---------------------------------------------------------------------------


class TestBuildServerSocket:
    def test_exits_on_no_listen_fds(self):
        with (
            patch.dict("os.environ", {"LISTEN_FDS": "0"}, clear=False),
            patch("sys.exit", side_effect=SystemExit(1)),
            pytest.raises(SystemExit),
        ):
            _build_server_socket("/opt/test/scripts/generated/install.sh")

    def test_exits_on_missing_listen_fds(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("sys.exit", side_effect=SystemExit(1)),
            pytest.raises(SystemExit),
        ):
            _build_server_socket("/opt/test/scripts/generated/install.sh")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class TestEntryPoint:
    def test_main_is_callable(self):
        from fraisier.scaffold_install_helper import main

        assert callable(main)


# ---------------------------------------------------------------------------
# Renderer integration: scaffold-install-helper units are generated
# ---------------------------------------------------------------------------


class TestScaffoldRendererGeneratesHelperUnits:
    """ScaffoldRenderer must emit the scaffold-install-helper .service and .socket."""

    def _make_config(self, tmp_path):
        from fraisier.config import FraisierConfig

        output_dir = tmp_path / "scripts" / "generated"
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            f"""
name: myproject
scaffold:
  output_dir: {output_dir}
  deploy_user: fraisier

fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        git_repo: /var/repos/api.git
"""
        )
        return FraisierConfig(str(config_file))

    def test_renders_scaffold_install_helper_service(self, tmp_path):
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        files = renderer.render(dry_run=True)
        assert any("scaffold-install-helper.service" in f for f in files), (
            f"scaffold-install-helper.service not in rendered files: {files}"
        )

    def test_renders_scaffold_install_helper_socket(self, tmp_path):
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        files = renderer.render(dry_run=True)
        assert any("scaffold-install-helper.socket" in f for f in files), (
            f"scaffold-install-helper.socket not in rendered files: {files}"
        )

    def test_service_file_content_references_install_script(self, tmp_path):
        """Service unit must reference the baked-in install.sh path."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render(dry_run=False)

        output_dir = renderer.output_dir
        service_file = (
            output_dir / "systemd" / "fraisier-myproject-scaffold-install-helper.service"
        )
        assert service_file.exists(), f"Expected {service_file} to exist"
        content = service_file.read_text()
        assert "install.sh" in content
        assert "fraisier-scaffold-install-helper" in content

    def test_socket_file_content_has_correct_socket_path(self, tmp_path):
        """Socket unit must have the correct ListenStream path."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render(dry_run=False)

        output_dir = renderer.output_dir
        socket_file = (
            output_dir / "systemd" / "fraisier-myproject-scaffold-install-helper.socket"
        )
        assert socket_file.exists(), f"Expected {socket_file} to exist"
        content = socket_file.read_text()
        assert "scaffold-install-myproject.sock" in content


# ---------------------------------------------------------------------------
# Cycle 4: install.sh.j2 installs the scaffold-install-helper units
# ---------------------------------------------------------------------------


class TestInstallShContainsScaffoldInstallHelper:
    """install.sh must copy and enable the scaffold-install-helper units."""

    def _render_install_sh(self, tmp_path):
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        output_dir = tmp_path / "generated"
        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(
            f"""
name: myproject
scaffold:
  output_dir: {output_dir}
  deploy_user: fraisier

fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        git_repo: /var/repos/api.git
"""
        )
        config = FraisierConfig(str(cfg_path))
        renderer = ScaffoldRenderer(config)
        renderer.render(dry_run=False)
        return (output_dir / "install.sh").read_text()

    def test_install_sh_references_scaffold_install_helper_service(self, tmp_path):
        content = self._render_install_sh(tmp_path)
        assert "scaffold-install-helper.service" in content

    def test_install_sh_references_scaffold_install_helper_socket(self, tmp_path):
        content = self._render_install_sh(tmp_path)
        assert "scaffold-install-helper.socket" in content

    def test_install_sh_enables_scaffold_install_helper(self, tmp_path):
        content = self._render_install_sh(tmp_path)
        assert "systemctl enable --now" in content
        assert "scaffold-install-helper" in content


# ---------------------------------------------------------------------------
# Cycle 5: Socket client in _install_scaffold()
# ---------------------------------------------------------------------------


class TestInstallScaffoldSocketClient:
    """_install_scaffold() tries the socket helper first, falls back to subprocess."""

    def _make_deployer(self, tmp_path):
        from fraisier.deployers.api import APIDeployer

        config_path = tmp_path / "fraises.yaml"
        config_path.write_text("name: testproject\nfraises: {}\n")
        deployer = APIDeployer({})
        return deployer, config_path

    def test_uses_socket_when_available(self, tmp_path):
        """When the socket helper succeeds, _install_scaffold does not call subprocess."""
        import types

        deployer, config_path = self._make_deployer(tmp_path)

        mock_runner = MagicMock()
        deployer.runner = mock_runner

        # Patch _try_scaffold_install_via_socket to simulate a reachable helper
        success_result = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(
            deployer, "_try_scaffold_install_via_socket", return_value=success_result
        ):
            deployer._install_scaffold(config_path=config_path)

        assert not mock_runner.run.called, "subprocess should not be called when socket succeeds"

    def test_falls_back_to_subprocess_when_no_socket(self, tmp_path):
        """When no socket exists, falls back to subprocess."""
        deployer, config_path = self._make_deployer(tmp_path)

        mock_runner = MagicMock()
        mock_runner.run.return_value = MagicMock(returncode=0, stdout="")
        deployer.runner = mock_runner

        # Patch socket path to a non-existent file
        with patch(
            "fraisier.deployers.base._get_scaffold_socket_path",
            return_value=str(tmp_path / "nonexistent.sock"),
        ):
            deployer._install_scaffold(config_path=config_path)

        assert mock_runner.run.called, "subprocess should be called as fallback"
        cmd = mock_runner.run.call_args[0][0]
        assert "scaffold-install" in " ".join(cmd)

    def test_raises_deployment_error_when_socket_returns_failure(self, tmp_path):
        """Socket helper returning ok=False must raise DeploymentError, not fall back."""
        import types

        from fraisier.errors import DeploymentError

        deployer, config_path = self._make_deployer(tmp_path)

        failure_result = types.SimpleNamespace(
            returncode=1, stdout="install.sh failed", stderr=""
        )
        with (
            patch.object(
                deployer,
                "_try_scaffold_install_via_socket",
                return_value=failure_result,
            ),
            pytest.raises(DeploymentError),
        ):
            deployer._install_scaffold(config_path=config_path)

"""Tests for fraisier.scaffold_install_helper — root-privileged scaffold helper."""

from __future__ import annotations

import json
import socket as _socket
from unittest.mock import MagicMock, patch

import pytest

from fraisier.root_policy import RootPolicy, UnitGrant, dump_policy
from fraisier.scaffold_apply import ApplyOutcome
from fraisier.scaffold_install_helper import (
    _build_server_socket,
    _handle_connection,
    _send_error,
    _send_response,
    _serve_connection,
    apply_for_project,
    build_apply,
)
from fraisier.unit_validator import Refusal

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


class _Recorder:
    def __init__(self, outcome: ApplyOutcome | None = None, error=None):
        self.outcome = outcome or ApplyOutcome(installed=["a.service"])
        self.error = error
        self.calls: list[bool] = []

    def __call__(self, deploy_in_flight: bool) -> ApplyOutcome:
        self.calls.append(deploy_in_flight)
        if self.error is not None:
            raise self.error
        return self.outcome


def _call(request, apply) -> dict:
    """Send *request* via socket pair, call handler, return parsed response."""
    server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
    payload = request if isinstance(request, bytes) else json.dumps(request).encode()
    client.sendall(payload + b"\n")
    client.shutdown(_socket.SHUT_WR)
    _handle_connection(server, apply)
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
        conn = MagicMock()
        conn.sendall.side_effect = OSError("broken pipe")
        _send_response(conn, {"ok": True})


# ---------------------------------------------------------------------------
# _handle_connection: the render is applied, never executed
# ---------------------------------------------------------------------------


class TestHandleConnection:
    def test_an_install_request_applies_and_reports(self):
        apply = _Recorder(
            ApplyOutcome(installed=["a.service"], unchanged=["b.service"])
        )
        result = _call({"action": "install"}, apply)
        assert result["ok"] is True
        assert result["returncode"] == 0
        assert result["installed"] == ["a.service"]
        assert result["stdout"] == "installed 1 unit(s), 1 unchanged: a.service"

    def test_nothing_is_executed_from_the_render(self):
        with patch("subprocess.run") as run:
            _call({"action": "install"}, _Recorder())
        run.assert_not_called()

    def test_a_declared_deploy_defers_restarts(self):
        apply = _Recorder()
        _call({"action": "install", "deploy_in_flight": True}, apply)
        assert apply.calls == [True]

    def test_an_undeclared_request_does_not(self):
        apply = _Recorder()
        _call({"action": "install"}, apply)
        assert apply.calls == [False]

    def test_non_boolean_declaration_is_not_trusted(self):
        apply = _Recorder()
        _call({"action": "install", "deploy_in_flight": "yes; rm -rf /"}, apply)
        assert apply.calls == [False]

    def test_a_refused_or_pending_apply_is_ok_false_with_every_reason(self):
        outcome = ApplyOutcome(
            refused={"app.service": [Refusal("user", "User='root' is not allowed")]},
            pending=["/etc/sudoers.d/demo: differs from the render"],
            deferred_restarts=["w.service"],
        )
        result = _call({"action": "install"}, _Recorder(outcome))
        assert result["ok"] is False
        assert result["returncode"] == 1
        assert result["pending"] == ["/etc/sudoers.d/demo: differs from the render"]
        assert result["refused"] == {
            "app.service": ["[user] User='root' is not allowed"]
        }
        assert "pending operator install: /etc/sudoers.d/demo" in result["stderr"]
        assert "sudo fraisier scaffold-install" in result["stderr"]
        assert result["deferred_restarts"] == ["w.service"]

    def test_unknown_action_is_rejected_without_applying(self):
        apply = _Recorder()
        result = _call({"action": "rm_rf"}, apply)
        assert result["ok"] is False
        assert "action not allowed" in result["error"]
        assert apply.calls == []

    def test_a_non_object_request_is_rejected(self):
        apply = _Recorder()
        result = _call([1, 2], apply)
        assert result["ok"] is False
        assert apply.calls == []

    def test_malformed_json_is_rejected(self):
        result = _call(b"not valid json", _Recorder())
        assert result["ok"] is False
        assert "malformed JSON" in result["error"]

    def test_empty_connection_is_handled_gracefully(self):
        server, client = _socket.socketpair(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.shutdown(_socket.SHUT_WR)
        _handle_connection(server, _Recorder())
        client.close()

    def test_an_apply_that_raises_is_an_error_response(self):
        result = _call({"action": "install"}, _Recorder(error=RuntimeError("disk")))
        assert result["ok"] is False
        assert "disk" in result["error"]


# ---------------------------------------------------------------------------
# argv: --project, and a unit that still names an install.sh
# ---------------------------------------------------------------------------


class TestBuildApply:
    def test_a_legacy_unit_never_runs_its_install_script(self, tmp_path):
        script = tmp_path / "install.sh"
        script.write_text("#!/bin/bash\ntouch " + str(tmp_path / "ran") + "\n")
        apply = build_apply([str(script)])
        outcome = apply(True)
        assert not outcome.ok
        assert "sudo fraisier scaffold-install" in outcome.pending[0]
        assert str(script) in outcome.pending[0]
        assert not (tmp_path / "ran").exists()

    def test_project_selects_the_policy(self):
        with patch(
            "fraisier.scaffold_install_helper.apply_for_project",
            return_value=ApplyOutcome(),
        ) as afp:
            build_apply(["--project", "demo"])(True)
        afp.assert_called_once_with("demo", True)


class TestApplyForProject:
    def _policy(self, scaffold_dir: str, project: str = "demo") -> RootPolicy:
        return RootPolicy(
            project=project,
            scaffold_dir=scaffold_dir,
            users=frozenset({"deploy"}),
            groups=frozenset(),
            exec_prefixes=("/opt/x/",),
            read_paths=frozenset(),
            directories=frozenset(),
            units={"a.service": UnitGrant("systemd/a.service", "plain")},
        )

    def test_an_untrusted_policy_applies_nothing_and_says_why(self, tmp_path):
        from fraisier.root_policy import RootPolicyError

        error = RootPolicyError(
            "no root policy at X: run `sudo fraisier scaffold-install`"
        )
        with patch("fraisier.scaffold_install_helper.load_policy", side_effect=error):
            outcome = apply_for_project("demo", True, policy_root=tmp_path)
        assert not outcome.ok
        assert outcome.pending == [str(error)]

    def test_a_policy_a_non_root_user_wrote_is_refused(self, tmp_path):
        """The suite does not run as root, so tmp_path is exactly that case."""
        policy_dir = tmp_path / "demo"
        policy_dir.mkdir()
        (policy_dir / "root-policy.json").write_text(
            dump_policy(self._policy(str(tmp_path)))
        )
        writes = []
        outcome = apply_for_project(
            "demo",
            True,
            policy_root=tmp_path,
            systemd_dir=tmp_path,
            systemctl=lambda *a: writes.append(a) or True,
        )
        assert not outcome.ok
        assert "other than root" in outcome.pending[0]
        assert writes == []

    def test_a_policy_for_another_project_is_refused(self, tmp_path):
        policy = self._policy(str(tmp_path), project="other")
        with patch("fraisier.scaffold_install_helper.load_policy", return_value=policy):
            outcome = apply_for_project("demo", True, policy_root=tmp_path)
        assert outcome.pending == ["the root policy is for 'other', not 'demo'"]

    def test_applies_the_tree_the_policy_names(self, tmp_path):
        tree = tmp_path / "scaffold"
        (tree / "systemd").mkdir(parents=True)
        unit = "[Service]\nUser=deploy\nExecStart=/opt/x/run\n"
        (tree / "systemd" / "a.service").write_text(unit)
        (tree / "artifact-manifest.json").write_text(
            json.dumps(
                {
                    "artifacts": [
                        {
                            "source": "systemd/a.service",
                            "destination": "/etc/systemd/system/a.service",
                            "disposition": "plain",
                        }
                    ],
                    "hosts": {"solo": {"scopes": [], "environments": []}},
                }
            )
        )
        systemd = tmp_path / "systemd-dir"
        systemd.mkdir()
        calls = []
        with patch(
            "fraisier.scaffold_install_helper.load_policy",
            return_value=self._policy(str(tree)),
        ):
            outcome = apply_for_project(
                "demo",
                False,
                policy_root=tmp_path,
                systemd_dir=systemd,
                systemctl=lambda *a: calls.append(a) or True,
                hostname=lambda: "solo",
            )
        assert outcome.ok, outcome.report()
        assert (systemd / "a.service").read_text() == unit
        assert calls == [("daemon-reload",)]


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
            _build_server_socket()

    def test_exits_on_missing_listen_fds(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("sys.exit", side_effect=SystemExit(1)),
            pytest.raises(SystemExit),
        ):
            _build_server_socket()


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

    def test_service_names_the_project_and_no_script(self, tmp_path):
        """The helper reads the root policy by project; it runs no script (#433)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render(dry_run=False)

        service_file = (
            renderer.output_dir
            / "systemd"
            / "fraisier-myproject-scaffold-install-helper.service"
        )
        content = service_file.read_text()
        assert "-m fraisier.scaffold_install_helper" in content
        assert "--project myproject" in content
        assert "install.sh" not in content

    def test_service_may_write_only_systemd_units(self, tmp_path):
        """nginx and sudoers are an operator's now, so the helper cannot write them."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render(dry_run=False)

        content = (
            renderer.output_dir
            / "systemd"
            / "fraisier-myproject-scaffold-install-helper.service"
        ).read_text()
        assert "ReadWritePaths=/etc/systemd/system\n" in content
        assert "ProtectSystem=strict" in content

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

        assert not mock_runner.run.called, (
            "subprocess should not be called when socket succeeds"
        )

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


# ---------------------------------------------------------------------------
# Phase 3 cycle 3.1 — SO_PEERCRED retrofit
# ---------------------------------------------------------------------------


class TestServeConnectionEnforcesPeerCreds:
    """``_serve_connection`` runs ``check_peer_creds`` before dispatching."""

    def test_rejects_non_matching_uid(self):
        import os

        server, client = _make_socket_pair()
        apply = _Recorder()
        _serve_connection(server, expected_uid=os.getuid() + 1, apply=apply)
        data = _recv_json(client)
        client.close()
        assert data["ok"] is False
        assert "peer" in data["error"].lower()
        assert apply.calls == []

    def test_without_a_deploy_uid_it_serves_nobody(self):
        server, client = _make_socket_pair()
        apply = _Recorder()
        _serve_connection(server, expected_uid=None, apply=apply)
        data = _recv_json(client)
        client.close()
        assert data["ok"] is False
        assert "peer credentials cannot be checked" in data["error"]
        assert apply.calls == []

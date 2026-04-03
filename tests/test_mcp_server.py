"""Tests for the fraisier MCP server."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

mcp_available = pytest.importorskip("mcp", reason="mcp package not installed")

from fraisier.mcp_server import (  # noqa: E402
    _load_config,
    _resolve_runner,
    bootstrap_preflight,
    bootstrap_server,
    mcp,
)


@pytest.fixture
def minimal_config(tmp_path):
    """Create a minimal fraises.yaml and set env for the MCP server."""
    p = tmp_path / "fraises.yaml"
    p.write_text(
        "name: myapp\n"
        "fraises:\n"
        "  api:\n"
        "    type: api\n"
        "    environments:\n"
        "      production: {}\n"
        "environments:\n"
        "  production:\n"
        "    server: prod.example.com\n"
        "scaffold:\n"
        "  deploy_user: myapp_deploy\n"
    )
    return p


class TestToolRegistration:
    def test_bootstrap_preflight_tool_registered(self):
        tools = {t.name for t in mcp._tool_manager.list_tools()}
        assert "bootstrap_preflight" in tools

    def test_bootstrap_server_tool_registered(self):
        tools = {t.name for t in mcp._tool_manager.list_tools()}
        assert "bootstrap_server" in tools


class TestLoadConfig:
    def test_loads_from_env_var(self, minimal_config):
        with patch.dict(os.environ, {"FRAISIER_CONFIG": str(minimal_config)}):
            config = _load_config()
        assert config.project_name == "myapp"

    def test_raises_without_config(self, tmp_path):
        with (
            patch.dict(os.environ, {"FRAISIER_CONFIG": str(tmp_path / "missing.yaml")}),
            pytest.raises((FileNotFoundError, OSError)),
        ):
            _load_config()


class TestResolveRunner:
    def test_resolves_server_from_config(self, minimal_config):
        with patch.dict(os.environ, {"FRAISIER_CONFIG": str(minimal_config)}):
            config = _load_config()

        with patch("fraisier.mcp_server.resolve_ssh_config") as mock_ssh:
            from fraisier.ssh_config import SSHHostConfig

            mock_ssh.return_value = SSHHostConfig()
            server, runner = _resolve_runner(config, "production")

        assert server == "prod.example.com"
        assert runner.host == "prod.example.com"

    def test_raises_for_missing_environment(self, minimal_config):
        with patch.dict(os.environ, {"FRAISIER_CONFIG": str(minimal_config)}):
            config = _load_config()

        with pytest.raises(ValueError, match="not set"):
            _resolve_runner(config, "staging")

    def test_passes_sudo_password(self, minimal_config):
        with patch.dict(os.environ, {"FRAISIER_CONFIG": str(minimal_config)}):
            config = _load_config()

        with patch("fraisier.mcp_server.resolve_ssh_config") as mock_ssh:
            from fraisier.ssh_config import SSHHostConfig

            mock_ssh.return_value = SSHHostConfig()
            _, runner = _resolve_runner(
                config,
                "production",
                use_sudo=True,
                sudo_password="secret",
            )

        assert runner.use_sudo is True
        assert runner.sudo_password == "secret"


class TestBootstrapPreflightTool:
    @pytest.mark.asyncio
    async def test_returns_check_results(self, minimal_config):
        from fraisier.preflight import CheckResult, PreflightResult

        mock_result = PreflightResult(
            server="prod.example.com",
            checks=[
                CheckResult(name="SSH connectivity", passed=True),
                CheckResult(
                    name="nginx installed",
                    passed=False,
                    fix_hint="apt install nginx",
                ),
            ],
        )

        with (
            patch.dict(os.environ, {"FRAISIER_CONFIG": str(minimal_config)}),
            patch("fraisier.mcp_server.PreflightChecker") as mock_pc,
            patch("fraisier.mcp_server.resolve_ssh_config") as mock_ssh,
        ):
            from fraisier.ssh_config import SSHHostConfig

            mock_ssh.return_value = SSHHostConfig()
            mock_pc.return_value.run_all.return_value = mock_result

            output = await bootstrap_preflight(environment="production")

        assert "SSH connectivity" in output
        assert "nginx" in output
        assert "FAIL" in output
        assert "1 check(s) failed" in output


class TestBootstrapServerTool:
    @pytest.mark.asyncio
    async def test_runs_bootstrap_without_sudo(self, minimal_config):
        from fraisier.bootstrap import BootstrapResult, StepResult

        mock_result = BootstrapResult(
            steps=[StepResult(name="Create deploy user", success=True)]
        )

        with (
            patch.dict(os.environ, {"FRAISIER_CONFIG": str(minimal_config)}),
            patch("fraisier.mcp_server.ServerBootstrapper") as mock_bs,
            patch("fraisier.mcp_server.resolve_ssh_config") as mock_ssh,
        ):
            from fraisier.ssh_config import SSHHostConfig

            mock_ssh.return_value = SSHHostConfig()
            mock_bs.return_value.bootstrap.return_value = mock_result

            output = await bootstrap_server(environment="production")

        assert "Create deploy user" in output
        assert "Bootstrap complete" in output

    @pytest.mark.asyncio
    async def test_elicits_password_when_sudo(self, minimal_config):
        from mcp.server.elicitation import AcceptedElicitation

        from fraisier.bootstrap import BootstrapResult, StepResult

        mock_result = BootstrapResult(
            steps=[StepResult(name="Create deploy user", success=True)]
        )

        from fraisier.mcp_server import SudoPasswordSchema

        mock_ctx = MagicMock()
        mock_ctx.elicit = AsyncMock(
            return_value=AcceptedElicitation(
                data=SudoPasswordSchema(sudo_password="secret")
            )
        )

        with (
            patch.dict(os.environ, {"FRAISIER_CONFIG": str(minimal_config)}),
            patch("fraisier.mcp_server.ServerBootstrapper") as mock_bs,
            patch("fraisier.mcp_server.SSHRunner") as mock_runner_cls,
            patch("fraisier.mcp_server.resolve_ssh_config") as mock_ssh,
        ):
            from fraisier.ssh_config import SSHHostConfig

            mock_ssh.return_value = SSHHostConfig()
            mock_bs.return_value.bootstrap.return_value = mock_result

            output = await bootstrap_server(
                environment="production", sudo=True, ctx=mock_ctx
            )

        mock_ctx.elicit.assert_called_once()
        runner_kwargs = mock_runner_cls.call_args[1]
        assert runner_kwargs["sudo_password"] == "secret"
        assert runner_kwargs["use_sudo"] is True
        assert "Bootstrap complete" in output

    @pytest.mark.asyncio
    async def test_aborts_when_elicitation_declined(self, minimal_config):
        from mcp.server.elicitation import DeclinedElicitation

        mock_ctx = MagicMock()
        mock_ctx.elicit = AsyncMock(return_value=DeclinedElicitation())

        with (
            patch.dict(os.environ, {"FRAISIER_CONFIG": str(minimal_config)}),
            patch("fraisier.mcp_server.resolve_ssh_config") as mock_ssh,
        ):
            from fraisier.ssh_config import SSHHostConfig

            mock_ssh.return_value = SSHHostConfig()

            output = await bootstrap_server(
                environment="production", sudo=True, ctx=mock_ctx
            )

        assert "aborted" in output.lower() or "declined" in output.lower()

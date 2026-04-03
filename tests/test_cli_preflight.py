"""Tests for the fraisier bootstrap-preflight CLI command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.preflight import CheckResult, PreflightResult

_YAML_WITH_SERVER = """\
name: myapp
fraises:
  api:
    type: api
    environments:
      production: {}
environments:
  production:
    server: prod.example.com
scaffold:
  deploy_user: myapp_deploy
"""

_YAML_NO_SERVER = """\
name: myapp
fraises:
  api:
    type: api
    environments:
      production: {}
scaffold:
  deploy_user: myapp_deploy
"""


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def config_file_with_server(tmp_path):
    p = tmp_path / "fraises.yaml"
    p.write_text(_YAML_WITH_SERVER)
    return p


@pytest.fixture
def config_file_no_server(tmp_path):
    p = tmp_path / "fraises.yaml"
    p.write_text(_YAML_NO_SERVER)
    return p


def _all_pass_result(server: str = "prod.example.com") -> PreflightResult:
    return PreflightResult(
        server=server,
        checks=[
            CheckResult(
                name="SSH connectivity",
                passed=True,
                message="port 22, user root",
            ),
            CheckResult(name="sudo available", passed=True),
            CheckResult(name="git installed", passed=True),
        ],
    )


def _mixed_result(server: str = "prod.example.com") -> PreflightResult:
    return PreflightResult(
        server=server,
        checks=[
            CheckResult(
                name="SSH connectivity",
                passed=True,
                message="port 22, user root",
            ),
            CheckResult(
                name="sudo available",
                passed=False,
                fix_hint="Grant sudo",
            ),
            CheckResult(
                name="nginx installed",
                passed=False,
                fix_hint="apt install nginx",
            ),
        ],
    )


class TestPreflightHelp:
    def test_help_exits_zero(self, runner):
        result = runner.invoke(main, ["bootstrap-preflight", "--help"])
        assert result.exit_code == 0

    def test_help_lists_environment_option(self, runner):
        result = runner.invoke(main, ["bootstrap-preflight", "--help"])
        assert "--environment" in result.output

    def test_help_lists_server_override(self, runner):
        result = runner.invoke(main, ["bootstrap-preflight", "--help"])
        assert "--server" in result.output

    def test_help_lists_ssh_options(self, runner):
        result = runner.invoke(main, ["bootstrap-preflight", "--help"])
        assert "--ssh-user" in result.output
        assert "--ssh-port" in result.output


class TestPreflightMissingServer:
    def test_error_when_server_not_configured(self, runner, config_file_no_server):
        result = runner.invoke(
            main,
            [
                "-c",
                str(config_file_no_server),
                "bootstrap-preflight",
                "-e",
                "production",
            ],
        )
        assert result.exit_code != 0
        assert "server" in result.output.lower()

    def test_server_flag_overrides_missing_config(self, runner, config_file_no_server):
        with patch("fraisier.preflight.PreflightChecker") as mock_cls:
            mock_cls.return_value.run_all.return_value = _all_pass_result()
            result = runner.invoke(
                main,
                [
                    "-c",
                    str(config_file_no_server),
                    "bootstrap-preflight",
                    "-e",
                    "production",
                    "--server",
                    "override.example.com",
                ],
            )
        assert result.exit_code == 0


class TestPreflightOutput:
    def test_all_pass_shows_success(self, runner, config_file_with_server):
        with patch("fraisier.preflight.PreflightChecker") as mock_cls:
            mock_cls.return_value.run_all.return_value = _all_pass_result()
            result = runner.invoke(
                main,
                [
                    "-c",
                    str(config_file_with_server),
                    "bootstrap-preflight",
                    "-e",
                    "production",
                ],
            )
        assert result.exit_code == 0

    def test_failures_show_hints(self, runner, config_file_with_server):
        with patch("fraisier.preflight.PreflightChecker") as mock_cls:
            mock_cls.return_value.run_all.return_value = _mixed_result()
            result = runner.invoke(
                main,
                [
                    "-c",
                    str(config_file_with_server),
                    "bootstrap-preflight",
                    "-e",
                    "production",
                ],
            )
        assert result.exit_code != 0
        assert "nginx" in result.output.lower() or "apt install" in result.output

    def test_failures_exit_nonzero(self, runner, config_file_with_server):
        with patch("fraisier.preflight.PreflightChecker") as mock_cls:
            mock_cls.return_value.run_all.return_value = _mixed_result()
            result = runner.invoke(
                main,
                [
                    "-c",
                    str(config_file_with_server),
                    "bootstrap-preflight",
                    "-e",
                    "production",
                ],
            )
        assert result.exit_code != 0


class TestPreflightSSHConfigResolution:
    def _capture_runner(self, config_file, cli_args, ssh_host_config=None):
        from fraisier.ssh_config import SSHHostConfig

        if ssh_host_config is None:
            ssh_host_config = SSHHostConfig()

        captured: list = []

        def fake_checker(**kwargs):
            from fraisier.runners import SSHRunner

            runner_arg = kwargs.get("runner")
            if isinstance(runner_arg, SSHRunner):
                captured.append(runner_arg)
            m = MagicMock()
            m.run_all.return_value = _all_pass_result()
            return m

        _pc_path = "fraisier.preflight.PreflightChecker"
        _ssh_path = "fraisier.ssh_config.resolve_ssh_config"
        with (
            patch(_pc_path, side_effect=fake_checker),
            patch(_ssh_path, return_value=ssh_host_config),
        ):
            cli_runner = CliRunner()
            result = cli_runner.invoke(
                main,
                [
                    "-c",
                    str(config_file),
                    "bootstrap-preflight",
                    "-e",
                    "production",
                    *cli_args,
                ],
            )

        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        return captured[0]

    def test_defaults_without_ssh_config(self, config_file_with_server):
        runner = self._capture_runner(config_file_with_server, [])
        assert runner.user == "root"
        assert runner.port == 22

    def test_ssh_config_provides_port(self, config_file_with_server):
        from fraisier.ssh_config import SSHHostConfig

        cfg = SSHHostConfig(port=2222)
        runner = self._capture_runner(config_file_with_server, [], ssh_host_config=cfg)
        assert runner.port == 2222

    def test_cli_user_overrides_ssh_config(self, config_file_with_server):
        from fraisier.ssh_config import SSHHostConfig

        cfg = SSHHostConfig(user="deployer")
        runner = self._capture_runner(
            config_file_with_server,
            ["--ssh-user", "admin"],
            ssh_host_config=cfg,
        )
        assert runner.user == "admin"

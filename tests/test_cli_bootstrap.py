"""Tests for the fraisier bootstrap CLI command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.bootstrap import BootstrapResult, StepResult
from fraisier.cli.main import main

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


class TestBootstrapHelp:
    def test_help_exits_zero(self, runner):
        result = runner.invoke(main, ["bootstrap", "--help"])
        assert result.exit_code == 0

    def test_help_lists_environment_option(self, runner):
        result = runner.invoke(main, ["bootstrap", "--help"])
        assert "--environment" in result.output

    def test_help_lists_dry_run(self, runner):
        result = runner.invoke(main, ["bootstrap", "--help"])
        assert "--dry-run" in result.output

    def test_help_lists_server_override(self, runner):
        result = runner.invoke(main, ["bootstrap", "--help"])
        assert "--server" in result.output

    def test_help_lists_ssh_user(self, runner):
        result = runner.invoke(main, ["bootstrap", "--help"])
        assert "--ssh-user" in result.output


class TestBootstrapMissingServer:
    def test_error_when_server_not_configured(self, runner, config_file_no_server):
        result = runner.invoke(
            main,
            ["-c", str(config_file_no_server), "bootstrap", "-e", "production"],
        )
        assert result.exit_code != 0
        assert "server" in result.output.lower()
        assert "production" in result.output

    def test_error_mentions_fraises_yaml(self, runner, config_file_no_server):
        result = runner.invoke(
            main,
            ["-c", str(config_file_no_server), "bootstrap", "-e", "production"],
        )
        assert "fraises.yaml" in result.output

    def test_server_flag_overrides_missing_config(self, runner, config_file_no_server):
        """--server bypasses the 'server not configured' error."""
        with patch("fraisier.bootstrap.ServerBootstrapper") as mock_bs_cls:
            mock_bs = MagicMock()
            mock_bs.bootstrap.return_value = BootstrapResult(
                steps=[StepResult(name="s", success=True)]
            )
            mock_bs_cls.return_value = mock_bs
            result = runner.invoke(
                main,
                [
                    "-c",
                    str(config_file_no_server),
                    "bootstrap",
                    "-e",
                    "production",
                    "--server",
                    "override.example.com",
                    "--yes",
                ],
            )
        assert result.exit_code == 0


class TestBootstrapServerResolution:
    def test_uses_server_from_environments_config(
        self, runner, config_file_with_server
    ):
        captured: list[str] = []

        def fake_bootstrapper(**kwargs):
            from fraisier.runners import SSHRunner

            runner_arg = kwargs.get("runner")
            if isinstance(runner_arg, SSHRunner):
                captured.append(runner_arg.host)
            m = MagicMock()
            m.bootstrap.return_value = BootstrapResult(
                steps=[StepResult(name="s", success=True)]
            )
            return m

        _bs_path = "fraisier.bootstrap.ServerBootstrapper"
        with patch(_bs_path, side_effect=fake_bootstrapper):
            runner_obj = CliRunner()
            runner_obj.invoke(
                main,
                [
                    "-c",
                    str(config_file_with_server),
                    "bootstrap",
                    "-e",
                    "production",
                    "--yes",
                ],
            )

        assert "prod.example.com" in captured

    def test_server_flag_takes_precedence(self, runner, config_file_with_server):
        captured: list[str] = []

        def fake_bootstrapper(**kwargs):
            from fraisier.runners import SSHRunner

            runner_arg = kwargs.get("runner")
            if isinstance(runner_arg, SSHRunner):
                captured.append(runner_arg.host)
            m = MagicMock()
            m.bootstrap.return_value = BootstrapResult(
                steps=[StepResult(name="s", success=True)]
            )
            return m

        _bs_path = "fraisier.bootstrap.ServerBootstrapper"
        with patch(_bs_path, side_effect=fake_bootstrapper):
            runner_obj = CliRunner()
            runner_obj.invoke(
                main,
                [
                    "-c",
                    str(config_file_with_server),
                    "bootstrap",
                    "-e",
                    "production",
                    "--server",
                    "other.example.com",
                    "--yes",
                ],
            )

        assert "other.example.com" in captured


class TestBootstrapDryRun:
    def test_dry_run_does_not_prompt(self, runner, config_file_with_server):
        with patch("fraisier.bootstrap.ServerBootstrapper") as mock_bs_cls:
            mock_bs = MagicMock()
            mock_bs.bootstrap.return_value = BootstrapResult(
                steps=[StepResult(name="step", success=True)]
            )
            mock_bs_cls.return_value = mock_bs
            result = runner.invoke(
                main,
                [
                    "-c",
                    str(config_file_with_server),
                    "bootstrap",
                    "-e",
                    "production",
                    "--dry-run",
                ],
                input="",  # no stdin input — prompt must not be shown
            )
        assert result.exit_code == 0

    def test_dry_run_passes_flag_to_bootstrapper(self, runner, config_file_with_server):
        captured: list[bool] = []

        def fake_bootstrapper(**kwargs):
            captured.append(kwargs.get("dry_run", False))
            m = MagicMock()
            m.bootstrap.return_value = BootstrapResult(
                steps=[StepResult(name="s", success=True)]
            )
            return m

        _bs_path = "fraisier.bootstrap.ServerBootstrapper"
        with patch(_bs_path, side_effect=fake_bootstrapper):
            runner.invoke(
                main,
                [
                    "-c",
                    str(config_file_with_server),
                    "bootstrap",
                    "-e",
                    "production",
                    "--dry-run",
                ],
            )

        assert captured == [True]


class TestBootstrapOutput:
    def _make_result(
        self, success: bool, step_name: str = "Create deploy user"
    ) -> BootstrapResult:
        return BootstrapResult(steps=[StepResult(name=step_name, success=success)])

    def _invoke(self, runner, config_file, *extra_args):
        args = ["-c", str(config_file), "bootstrap", "-e", "production", "--yes"]
        return runner.invoke(main, [*args, *extra_args])

    def test_success_shows_complete_message(self, runner, config_file_with_server):
        with patch("fraisier.bootstrap.ServerBootstrapper") as mock_bs_cls:
            mock_bs_cls.return_value.bootstrap.return_value = self._make_result(True)
            result = self._invoke(runner, config_file_with_server)
        assert result.exit_code == 0
        assert "Bootstrap complete" in result.output

    def test_failure_shows_error_details(self, runner, config_file_with_server):
        failed = BootstrapResult(
            steps=[
                StepResult(
                    name="Install uv",
                    success=False,
                    error="curl failed",
                    command="curl ...",
                ),
            ]
        )
        with patch("fraisier.bootstrap.ServerBootstrapper") as mock_bs_cls:
            mock_bs_cls.return_value.bootstrap.return_value = failed
            result = self._invoke(runner, config_file_with_server)
        assert result.exit_code != 0
        assert "curl failed" in result.output
        assert "Aborting" in result.output

    def test_environment_missing_fails_clearly(self, runner, config_file_with_server):
        result = runner.invoke(
            main,
            ["-c", str(config_file_with_server), "bootstrap"],
        )
        assert result.exit_code != 0
        assert (
            "--environment" in result.output or "environment" in result.output.lower()
        )

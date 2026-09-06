"""Smoke tests for the 9 core fraisier CLI commands.

Covers ``--help`` (exit 0) and at least one error path per command.
``validate-setup`` and ``diagnose`` call systemd helpers that are unreachable
without a live systemd socket; those helper branches are below 50% by design
and documented inline.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

from fraisier.cli.main import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cfg(tmp_path: Path) -> str:
    """Minimal fraises.yaml for CLI smoke tests."""
    p = tmp_path / "fraises.yaml"
    p.write_text(
        """
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  my_api:
    type: api
    description: Test API
    environments:
      production:
        app_path: /tmp/api-prod
        systemd_service: api-prod.service
        git_repo: https://github.com/test/api.git
        branch: main
"""
    )
    return str(p)


# ── init ──────────────────────────────────────────────────────────────────────


class TestInitCommand:
    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["init", "--help"])
        assert result.exit_code == 0

    def test_creates_file(self, runner: CliRunner, tmp_path: Path) -> None:
        out = tmp_path / "new"
        out.mkdir()
        result = runner.invoke(main, ["init", "--output", str(out)])
        assert result.exit_code == 0
        assert (out / "fraises.yaml").exists()

    def test_refuses_overwrite_without_force(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        out = tmp_path / "existing"
        out.mkdir()
        (out / "fraises.yaml").write_text("existing")
        result = runner.invoke(main, ["init", "--output", str(out)])
        assert result.exit_code != 0

    def test_force_overwrites_existing(self, runner: CliRunner, tmp_path: Path) -> None:
        out = tmp_path / "overwrite"
        out.mkdir()
        (out / "fraises.yaml").write_text("old")
        result = runner.invoke(main, ["init", "--output", str(out), "--force"])
        assert result.exit_code == 0

    def test_template_django(self, runner: CliRunner, tmp_path: Path) -> None:
        out = tmp_path / "django"
        out.mkdir()
        result = runner.invoke(
            main, ["init", "--output", str(out), "--template", "django"]
        )
        assert result.exit_code == 0


# ── list ──────────────────────────────────────────────────────────────────────


class TestListCommand:
    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["list", "--help"])
        assert result.exit_code == 0

    def test_tree_view_with_config(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(main, ["--config", cfg, "list"])
        assert result.exit_code == 0

    def test_flat_view_with_config(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(main, ["--config", cfg, "list", "--flat"])
        assert result.exit_code == 0


# ── status ────────────────────────────────────────────────────────────────────


class TestStatusCommand:
    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["status", "--help"])
        assert result.exit_code == 0

    def test_fraise_without_env_errors(self, runner: CliRunner, cfg: str) -> None:
        # Providing fraise but not environment is rejected
        result = runner.invoke(main, ["--config", cfg, "status", "my_api"])
        assert result.exit_code != 0

    def test_unknown_fraise_env_errors(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(
            main, ["--config", cfg, "status", "nonexistent", "production"]
        )
        assert result.exit_code != 0

    def test_global_all_servers(self, runner: CliRunner, cfg: str, test_db) -> None:
        # Exercises _show_global_status; deployer calls may raise (caught by handler)
        result = runner.invoke(main, ["--config", cfg, "status", "--all"])
        assert result.exit_code in (0, 1)

    def test_single_fraise_status(self, runner: CliRunner, cfg: str, test_db) -> None:
        # Exercises _show_single_status; deployer calls may raise (caught by handler)
        result = runner.invoke(
            main, ["--config", cfg, "status", "my_api", "production"]
        )
        assert result.exit_code in (0, 1)


# ── deploy-daemon ─────────────────────────────────────────────────────────────


class TestDeployDaemonCommand:
    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["deploy-daemon", "--help"])
        assert result.exit_code == 0

    def test_empty_stdin_errors(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(
            main,
            ["--config", cfg, "deploy-daemon", "--project", "my_api"],
            input="",
        )
        assert result.exit_code != 0

    def test_invalid_json_errors(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(
            main,
            ["--config", cfg, "deploy-daemon", "--project", "my_api"],
            input="not-valid-json",
        )
        assert result.exit_code != 0

    def test_project_mismatch_errors(self, runner: CliRunner, cfg: str) -> None:
        # Parses OK but project field doesn't match --project flag
        valid_request = (
            '{"version":1,"project":"other","environment":"production",'
            '"branch":"main","timestamp":"2026-04-11T00:00:00",'
            '"triggered_by":"test"}'
        )
        result = runner.invoke(
            main,
            ["--config", cfg, "deploy-daemon", "--project", "my_api"],
            input=valid_request,
        )
        assert result.exit_code != 0

    @patch("fraisier.daemon.execute_deployment_request")
    @patch("fraisier.daemon.parse_deployment_request")
    def test_deploy_success(
        self,
        mock_parse: MagicMock,
        mock_execute: MagicMock,
        runner: CliRunner,
        cfg: str,
    ) -> None:
        from fraisier.daemon import DeploymentRequest, DeploymentResult

        mock_parse.return_value = DeploymentRequest(
            version=1,
            project="my_api",
            environment="production",
            branch="main",
            timestamp="2026-04-11T00:00:00",
            triggered_by="test",
            options={},
            metadata={},
        )
        mock_execute.return_value = DeploymentResult(
            success=True,
            status="success",
            message="Deployed OK",
            deployed_version="abc1234",
        )
        result = runner.invoke(
            main,
            ["--config", cfg, "deploy-daemon", "--project", "my_api"],
            input="dummy",
        )
        assert result.exit_code == 0

    @patch("fraisier.daemon.execute_deployment_request")
    @patch("fraisier.daemon.parse_deployment_request")
    def test_deploy_failure_result(
        self,
        mock_parse: MagicMock,
        mock_execute: MagicMock,
        runner: CliRunner,
        cfg: str,
    ) -> None:
        from fraisier.daemon import DeploymentRequest, DeploymentResult

        mock_parse.return_value = DeploymentRequest(
            version=1,
            project="my_api",
            environment="production",
            branch="main",
            timestamp="2026-04-11T00:00:00",
            triggered_by="test",
            options={},
            metadata={},
        )
        mock_execute.return_value = DeploymentResult(
            success=False,
            status="failed",
            error_message="Build exploded",
        )
        result = runner.invoke(
            main,
            ["--config", cfg, "deploy-daemon", "--project", "my_api"],
            input="dummy",
        )
        assert result.exit_code != 0


# ── trigger-deploy ────────────────────────────────────────────────────────────


class TestTriggerDeployCommand:
    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["trigger-deploy", "--help"])
        assert result.exit_code == 0

    def test_unknown_fraise_errors(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(
            main, ["--config", cfg, "trigger-deploy", "nonexistent", "production"]
        )
        assert result.exit_code != 0

    def test_socket_not_present_errors(self, runner: CliRunner, cfg: str) -> None:
        # fraise/env exists but the unix socket is not present on this host
        result = runner.invoke(
            main, ["--config", cfg, "trigger-deploy", "my_api", "production"]
        )
        assert result.exit_code != 0  # FileNotFoundError → exit 1


# ── deployment-status ─────────────────────────────────────────────────────────


class TestDeploymentStatusCommand:
    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["deployment-status", "--help"])
        assert result.exit_code == 0

    def test_unknown_fraise_errors(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(
            main, ["--config", cfg, "deployment-status", "nonexistent"]
        )
        assert result.exit_code != 0

    def test_no_status_file_exits_zero(self, runner: CliRunner, cfg: str) -> None:
        # Fraise exists but no /run/fraisier/*.last_deployment file → friendly message
        result = runner.invoke(main, ["--config", cfg, "deployment-status", "my_api"])
        assert result.exit_code == 0
        assert "No deployment status found" in result.output

    @patch("fraisier.cli._deploy.Path")
    def test_status_file_displays_success(
        self, mock_path_cls: MagicMock, runner: CliRunner, cfg: str
    ) -> None:
        status_data = json.dumps(
            {
                "status": "success",
                "project": "my_api",
                "deployed_version": "abc1234",
                "deployed_at": "2026-04-11T00:00:00",
                "duration_seconds": 12.5,
            }
        )
        mock_status_path = MagicMock()
        mock_status_path.exists.return_value = True
        mock_status_path.read_text.return_value = status_data
        mock_run_dir = MagicMock()
        mock_run_dir.__truediv__.return_value = mock_status_path
        mock_path_cls.return_value = mock_run_dir

        result = runner.invoke(main, ["--config", cfg, "deployment-status", "my_api"])
        assert result.exit_code == 0

    @patch("fraisier.cli._deploy.Path")
    def test_permission_denied_gives_hint_not_traceback(
        self, mock_path_cls: MagicMock, runner: CliRunner, cfg: str
    ) -> None:
        # /run/fraisier is only readable by the deploy user; Path.exists()
        # propagates PermissionError for everyone else (#326)
        mock_status_path = MagicMock()
        mock_status_path.exists.side_effect = PermissionError(13, "Permission denied")
        mock_run_dir = MagicMock()
        mock_run_dir.__truediv__.return_value = mock_status_path
        mock_path_cls.return_value = mock_run_dir

        result = runner.invoke(main, ["--config", cfg, "deployment-status", "my_api"])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Permission denied" in result.output
        assert "deploy user" in result.output


# ── validate-setup ────────────────────────────────────────────────────────────
# Note: systemd/socket helper functions (_check_socket_directory, _check_socket_file,
# _check_socket_permissions, _check_systemd_units, _check_user_permissions) operate
# on /run/fraisier paths and live systemd units that are absent in CI.  Those
# branches remain below 50%.  The command entry-point and error-path guards are
# covered by the tests below.


class TestValidateSetupCommand:
    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["validate-setup", "--help"])
        assert result.exit_code == 0

    def test_unknown_fraise_errors(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(
            main, ["--config", cfg, "validate-setup", "nonexistent", "production"]
        )
        assert result.exit_code != 0

    def test_unknown_env_errors(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(
            main, ["--config", cfg, "validate-setup", "my_api", "nonexistent_env"]
        )
        assert result.exit_code != 0

    def test_runs_end_to_end(self, runner: CliRunner, cfg: str) -> None:
        # Systemd checks will fail (not a systemd host) but command must not crash
        result = runner.invoke(
            main, ["--config", cfg, "validate-setup", "my_api", "production"]
        )
        assert result.exit_code in (0, 1)

    def test_json_output(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(
            main, ["--config", cfg, "validate-setup", "my_api", "production", "--json"]
        )
        assert result.exit_code in (0, 1)
        assert "fraise" in result.output


# ── diagnose ──────────────────────────────────────────────────────────────────
# Same note as validate-setup: socket helpers require a live unix socket.
# _diagnose_socket_connectivity, _diagnose_systemd_service, and
# _diagnose_systemd_socket_unit operate on /run/fraisier and systemctl — absent
# in CI.  Entry-point and error guards are covered below.


class TestDiagnoseCommand:
    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["diagnose", "--help"])
        assert result.exit_code == 0

    def test_unknown_fraise_errors(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(
            main, ["--config", cfg, "diagnose", "nonexistent", "production"]
        )
        assert result.exit_code != 0

    def test_runs_end_to_end(self, runner: CliRunner, cfg: str) -> None:
        # Diagnose runs its checks (all will report failures) and exits 0
        result = runner.invoke(
            main, ["--config", cfg, "diagnose", "my_api", "production"]
        )
        assert result.exit_code == 0

    def test_json_output(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(
            main,
            ["--config", cfg, "diagnose", "my_api", "production", "--json"],
        )
        assert result.exit_code == 0
        assert "fraise" in result.output


# ── rollback ──────────────────────────────────────────────────────────────────


class TestRollbackCommand:
    def test_help(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["rollback", "--help"])
        assert result.exit_code == 0

    def test_unknown_fraise_errors(self, runner: CliRunner, cfg: str) -> None:
        result = runner.invoke(
            main, ["--config", cfg, "rollback", "nonexistent", "production"]
        )
        assert result.exit_code != 0

    def test_no_deployer_rollback_errors(self, runner: CliRunner, cfg: str) -> None:
        # Type "api" has no deployer → rollback not supported
        result = runner.invoke(
            main,
            ["--config", cfg, "rollback", "my_api", "production", "--force"],
        )
        assert result.exit_code != 0

    def test_failed_rollback_exits_non_zero(self, runner: CliRunner, cfg: str) -> None:
        """A rollback that did not restore a working service must not exit 0.

        The case that reaches here is a restore onto a version that fails its
        health check: it used to report ``success=True`` and exit 0 while
        nothing was serving (#378).
        """
        from unittest.mock import MagicMock, patch

        from fraisier.deployers.base import DeploymentResult, DeploymentStatus

        deployer = MagicMock()
        deployer.get_current_version.return_value = "newsha12"
        deployer.rollback.return_value = DeploymentResult(
            success=False,
            status=DeploymentStatus.ROLLBACK_FAILED,
            error_message=(
                "Reverted to aaaaaaaa, but the health check still fails "
                "after the restart."
            ),
        )

        with patch("fraisier.cli._rollback._get_deployer", return_value=deployer):
            result = runner.invoke(
                main,
                [
                    "--config",
                    cfg,
                    "rollback",
                    "my_api",
                    "production",
                    "--to-version",
                    "a" * 40,
                    "--force",
                ],
            )

        deployer.rollback.assert_called_once()
        assert result.exit_code != 0
        assert "rollback" in result.output.lower()

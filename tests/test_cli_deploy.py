"""Tests for CLI deploy, rollback, status, and list commands."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.deployers.base import DeploymentResult, DeploymentStatus


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_config():
    """Mock get_config to return a realistic config object."""
    config = MagicMock()
    config.get_fraise_environment.return_value = {
        "type": "api",
        "app_path": "/var/www/api",
        "systemd_service": "api.service",
        "health_check": {"url": "http://localhost:8000/health", "timeout": 10},
        "database": {"name": "mydb", "strategy": "migrate"},
    }
    config.list_fraises_detailed.return_value = [
        {
            "name": "my_api",
            "type": "api",
            "description": "Test API",
            "environments": ["production"],
        }
    ]
    config.deployment = MagicMock()
    config.deployment.get_strategy.return_value = "migrate"
    config._config = {"deployment": {}}
    with patch("fraisier.cli.main.get_config", return_value=config):
        yield config


def _success_result():
    return DeploymentResult(
        success=True,
        status=DeploymentStatus.SUCCESS,
        old_version="abc123",
        new_version="def456",
        duration_seconds=2.5,
    )


def _failure_result():
    return DeploymentResult(
        success=False,
        status=DeploymentStatus.FAILED,
        old_version="abc123",
        error_message="Health check timed out",
        duration_seconds=30.0,
    )


class TestDeploy:
    """Tests for the deploy command."""

    def test_deploy_selects_api_deployer(self, runner, mock_config):
        """deploy with api type instantiates APIDeployer."""
        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = True
        mock_deployer.execute.return_value = _success_result()

        with (
            patch("fraisier.cli.main._get_deployer", return_value=mock_deployer),
            patch("fraisier.locking.deployment_lock"),
        ):
            result = runner.invoke(main, ["deploy", "my_api", "production"])

        assert result.exit_code == 0
        assert "successful" in result.output.lower()
        mock_deployer.execute.assert_called_once()

    def test_deploy_acquires_lock(self, runner, mock_config):
        """deploy acquires a deployment lock before executing."""
        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = True
        mock_deployer.execute.return_value = _success_result()

        with (
            patch("fraisier.cli.main._get_deployer", return_value=mock_deployer),
            patch("fraisier.locking.deployment_lock") as mock_lock,
        ):
            runner.invoke(main, ["deploy", "my_api", "production"])

        mock_lock.assert_called_once_with("my_api")

    def test_deploy_dry_run_does_not_execute(self, runner, mock_config):
        """deploy --dry-run prints plan but does not call deployer."""
        with patch("fraisier.cli.main._get_deployer") as mock_get:
            result = runner.invoke(
                main, ["deploy", "my_api", "production", "--dry-run"]
            )

        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        mock_get.assert_not_called()

    def test_deploy_unknown_fraise_exits_1(self, runner, mock_config):
        """deploy with unknown fraise/env exits with error."""
        mock_config.get_fraise_environment.return_value = None

        result = runner.invoke(main, ["deploy", "nope", "production"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_deploy_failure_exits_1(self, runner, mock_config):
        """deploy failure prints error and exits 1."""
        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = True
        mock_deployer.execute.return_value = _failure_result()

        with (
            patch("fraisier.cli.main._get_deployer", return_value=mock_deployer),
            patch("fraisier.locking.deployment_lock"),
        ):
            result = runner.invoke(main, ["deploy", "my_api", "production"])

        assert result.exit_code == 1
        assert "failed" in result.output.lower()

    def test_deploy_up_to_date_skips(self, runner, mock_config):
        """deploy when already up-to-date skips execution."""
        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = False
        mock_deployer.get_current_version.return_value = "abc123"

        with patch("fraisier.cli.main._get_deployer", return_value=mock_deployer):
            result = runner.invoke(main, ["deploy", "my_api", "production"])

        assert result.exit_code == 0
        assert "up to date" in result.output.lower()
        mock_deployer.execute.assert_not_called()

    def test_deploy_force_deploys_even_when_up_to_date(self, runner, mock_config):
        """deploy --force deploys even if versions match."""
        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = False
        mock_deployer.execute.return_value = _success_result()

        with (
            patch("fraisier.cli.main._get_deployer", return_value=mock_deployer),
            patch("fraisier.locking.deployment_lock"),
        ):
            result = runner.invoke(main, ["deploy", "my_api", "production", "--force"])

        assert result.exit_code == 0
        mock_deployer.execute.assert_called_once()

    def test_deploy_unknown_type_exits_1(self, runner, mock_config):
        """deploy with unknown fraise type exits with error."""
        mock_config.get_fraise_environment.return_value = {"type": "unknown_type"}

        with patch("fraisier.cli.main._get_deployer", return_value=None):
            result = runner.invoke(main, ["deploy", "my_api", "production"])

        assert result.exit_code == 1
        assert "unknown" in result.output.lower()

    def test_deploy_etl_type(self, runner, mock_config):
        """deploy with etl type selects ETLDeployer."""
        mock_config.get_fraise_environment.return_value = {
            "type": "etl",
            "app_path": "/var/etl",
            "script_path": "run.py",
        }
        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = True
        mock_deployer.execute.return_value = _success_result()

        with (
            patch("fraisier.cli.main._get_deployer", return_value=mock_deployer),
            patch("fraisier.locking.deployment_lock"),
        ):
            result = runner.invoke(main, ["deploy", "my_api", "production"])

        assert result.exit_code == 0

    def test_deploy_no_rollback_sets_allow_irreversible(self, runner, mock_config):
        """deploy --no-rollback sets allow_irreversible in config."""
        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = True
        mock_deployer.execute.return_value = _success_result()

        with (
            patch("fraisier.cli.main._get_deployer", return_value=mock_deployer) as m,
            patch("fraisier.locking.deployment_lock"),
        ):
            runner.invoke(main, ["deploy", "my_api", "production", "--no-rollback"])

        # The fraise_config dict should have allow_irreversible=True
        call_args = m.call_args
        fraise_cfg = call_args[0][1]
        assert fraise_cfg.get("allow_irreversible") is True


class TestRollback:
    """Tests for the rollback command."""

    def test_rollback_calls_deployer_rollback(self, runner, mock_config, test_db):
        """rollback calls deployer.rollback() with correct version."""
        mock_deployer = MagicMock()
        mock_deployer.get_current_version.return_value = "def456"
        mock_deployer.rollback.return_value = _success_result()

        # Set up deployment history
        d1 = test_db.start_deployment(fraise="my_api", environment="production")
        test_db.complete_deployment(
            deployment_id=d1,
            success=True,
            new_version="abc123",
        )
        d2 = test_db.start_deployment(fraise="my_api", environment="production")
        test_db.complete_deployment(
            deployment_id=d2,
            success=True,
            new_version="def456",
        )

        with patch("fraisier.cli.main._get_deployer", return_value=mock_deployer):
            result = runner.invoke(
                main, ["rollback", "my_api", "production", "--force"]
            )

        assert result.exit_code == 0
        mock_deployer.rollback.assert_called_once()

    def test_rollback_with_to_version(self, runner, mock_config):
        """rollback --to-version passes explicit target."""
        mock_deployer = MagicMock()
        mock_deployer.get_current_version.return_value = "def456"
        mock_deployer.rollback.return_value = _success_result()

        with patch("fraisier.cli.main._get_deployer", return_value=mock_deployer):
            result = runner.invoke(
                main,
                [
                    "rollback",
                    "my_api",
                    "production",
                    "--to-version",
                    "abc123",
                    "--force",
                ],
            )

        assert result.exit_code == 0
        mock_deployer.rollback.assert_called_once_with(to_version="abc123")

    def test_rollback_no_previous_version_exits_1(self, runner, mock_config, test_db):
        """rollback with no history exits with error."""
        mock_deployer = MagicMock()
        mock_deployer.get_current_version.return_value = "abc123"

        with patch("fraisier.cli.main._get_deployer", return_value=mock_deployer):
            result = runner.invoke(
                main, ["rollback", "my_api", "production", "--force"]
            )

        assert result.exit_code == 1
        assert "no previous version" in result.output.lower()

    def test_rollback_unknown_fraise_exits_1(self, runner, mock_config):
        """rollback with unknown fraise/env exits with error."""
        mock_config.get_fraise_environment.return_value = None

        result = runner.invoke(main, ["rollback", "nope", "production", "--force"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()


class TestStatus:
    """Tests for the status command."""

    def test_status_displays_info(self, runner, mock_config, test_db):
        """status displays version and health information."""
        mock_deployer = MagicMock()
        mock_deployer.get_current_version.return_value = "abc123"
        mock_deployer.get_latest_version.return_value = "abc123"
        mock_deployer.health_check.return_value = True
        mock_deployer.is_deployment_needed.return_value = False

        with patch("fraisier.cli.main._get_deployer", return_value=mock_deployer):
            result = runner.invoke(main, ["status", "my_api", "production"])

        assert result.exit_code == 0
        assert "my_api" in result.output
        assert "production" in result.output

    def test_status_unknown_fraise_exits_1(self, runner, mock_config):
        """status with unknown fraise exits with error."""
        mock_config.get_fraise_environment.return_value = None

        result = runner.invoke(main, ["status", "nope", "production"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_status_no_args_shows_global_table(self, mock_config, test_db):
        """status with no args shows global table across all fraises."""
        mock_config.list_all_deployments.return_value = [
            {
                "fraise": "my_api",
                "environment": "production",
                "job": None,
                "type": "api",
                "name": "my_api",
            }
        ]
        mock_deployer = MagicMock()
        mock_deployer.get_current_version.return_value = "abc1234"
        mock_deployer.get_latest_version.return_value = "abc1234"
        mock_deployer.health_check.return_value = True

        runner = CliRunner(env={"COLUMNS": "200"})
        with patch("fraisier.cli.main._get_deployer", return_value=mock_deployer):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        # Check for table content and key data
        assert "my_api" in result.output
        assert "production" in result.output
        assert "abc1234" in result.output
        assert "deployed" in result.output.lower()
        assert "healthy" in result.output.lower()

    def test_status_global_deployed(self, mock_config, test_db):
        """status global view shows deployed when versions match."""
        mock_config.list_all_deployments.return_value = [
            {
                "fraise": "my_api",
                "environment": "production",
                "job": None,
                "type": "api",
                "name": "my_api",
            }
        ]
        mock_deployer = MagicMock()
        mock_deployer.get_current_version.return_value = "abc1234"
        mock_deployer.get_latest_version.return_value = "abc1234"
        mock_deployer.health_check.return_value = True

        runner = CliRunner(env={"COLUMNS": "200"})
        with patch("fraisier.cli.main._get_deployer", return_value=mock_deployer):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "deployed" in result.output.lower()

    def test_status_global_out_of_date(self, mock_config, test_db):
        """status global view shows out-of-date when versions differ."""
        mock_config.list_all_deployments.return_value = [
            {
                "fraise": "my_api",
                "environment": "production",
                "job": None,
                "type": "api",
                "name": "my_api",
            }
        ]
        mock_deployer = MagicMock()
        mock_deployer.get_current_version.return_value = "abc1234"
        mock_deployer.get_latest_version.return_value = "def5678"
        mock_deployer.health_check.return_value = True

        runner = CliRunner(env={"COLUMNS": "200"})
        with patch("fraisier.cli.main._get_deployer", return_value=mock_deployer):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "out-of-date" in result.output.lower()

    def test_status_global_health_not_configured(self, mock_config, test_db):
        """status global view shows not configured when health check not in config."""
        config_with_no_health = {
            "type": "api",
            "app_path": "/var/www/api",
            "systemd_service": "api.service",
            "database": {"name": "mydb", "strategy": "migrate"},
        }
        mock_config.get_fraise_environment.return_value = config_with_no_health
        mock_config.list_all_deployments.return_value = [
            {
                "fraise": "my_api",
                "environment": "production",
                "job": None,
                "type": "api",
                "name": "my_api",
            }
        ]
        mock_deployer = MagicMock()
        mock_deployer.get_current_version.return_value = "abc1234"
        mock_deployer.get_latest_version.return_value = "abc1234"

        runner = CliRunner(env={"COLUMNS": "200"})
        with patch("fraisier.cli.main._get_deployer", return_value=mock_deployer):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "not configured" in result.output.lower()

    def test_status_global_unhealthy(self, mock_config, test_db):
        """status global view shows unhealthy when health check fails."""
        mock_config.list_all_deployments.return_value = [
            {
                "fraise": "my_api",
                "environment": "production",
                "job": None,
                "type": "api",
                "name": "my_api",
            }
        ]
        mock_deployer = MagicMock()
        mock_deployer.get_current_version.return_value = "abc1234"
        mock_deployer.get_latest_version.return_value = "abc1234"
        mock_deployer.health_check.return_value = False

        runner = CliRunner(env={"COLUMNS": "200"})
        with patch("fraisier.cli.main._get_deployer", return_value=mock_deployer):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "unhealthy" in result.output.lower()

    def test_status_fraise_without_env_exits_1(self, mock_config):
        """status with fraise but no environment exits with error."""
        runner = CliRunner()
        result = runner.invoke(main, ["status", "my_api"])

        assert result.exit_code == 1
        assert "both fraise and environment required together" in result.output.lower()


class TestList:
    """Tests for the list command."""

    def test_list_shows_fraises(self, runner, mock_config):
        """list displays registered fraises."""
        result = runner.invoke(main, ["list"])

        assert result.exit_code == 0
        assert "my_api" in result.output

    def test_list_flat_shows_table(self, runner, mock_config):
        """list --flat shows flat table view."""
        mock_config.list_all_deployments.return_value = [
            {
                "fraise": "my_api",
                "environment": "production",
                "job": None,
                "type": "api",
                "name": "my_api",
            }
        ]

        result = runner.invoke(main, ["list", "--flat"])

        assert result.exit_code == 0
        assert "my_api" in result.output

"""Tests for daemon JSON parsing and deployment execution."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.daemon import (
    DeploymentRequest,
    execute_deployment_request,
    parse_deployment_request,
)


@pytest.fixture
def runner():
    return CliRunner()


class TestDeploymentRequest:
    """Tests for DeploymentRequest dataclass and parsing."""

    def test_parse_valid_request(self):
        """Parse valid JSON deployment request."""
        json_data = {
            "version": 1,
            "project": "api",
            "environment": "development",
            "branch": "dev",
            "timestamp": "2026-04-02T11:15:23Z",
            "triggered_by": "webhook",
            "options": {"force": False, "no_cache": False, "skip_health_check": False},
            "metadata": {
                "github_event": "push",
                "github_sender": "user",
                "webhook_id": "12345",
            },
        }

        request = parse_deployment_request(json.dumps(json_data))

        assert request.version == 1
        assert request.project == "api"
        assert request.environment == "development"
        assert request.branch == "dev"
        assert request.timestamp == "2026-04-02T11:15:23Z"
        assert request.triggered_by == "webhook"
        assert request.options == {
            "force": False,
            "no_cache": False,
            "skip_health_check": False,
        }
        assert request.metadata == {
            "github_event": "push",
            "github_sender": "user",
            "webhook_id": "12345",
        }

    def test_parse_missing_required_field(self):
        """Parse invalid JSON with missing required field."""
        json_data = {
            "version": 1,
            # Missing "project"
            "environment": "development",
            "branch": "dev",
        }

        with pytest.raises(ValueError, match="Missing required field: project"):
            parse_deployment_request(json.dumps(json_data))

    def test_parse_invalid_json(self):
        """Parse invalid JSON string."""
        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_deployment_request("invalid json")

    def test_parse_wrong_version(self):
        """Parse JSON with unsupported version."""
        json_data = {
            "version": 2,  # Unsupported version
            "project": "api",
            "environment": "development",
            "branch": "dev",
        }

        with pytest.raises(ValueError, match="Unsupported version"):
            parse_deployment_request(json.dumps(json_data))

    def test_parse_invalid_environment(self):
        """Parse JSON with invalid environment name."""
        json_data = {
            "version": 1,
            "project": "api",
            "environment": "invalid_env",
            "branch": "dev",
            "timestamp": "2026-04-02T11:15:23Z",
            "triggered_by": "webhook",
        }

        with pytest.raises(ValueError, match="Invalid environment"):
            parse_deployment_request(json.dumps(json_data))


class TestExecuteDeploymentRequest:
    """Tests for execute_deployment_request function."""

    @patch("fraisier.locking.deployment_lock")
    @patch("fraisier.daemon.get_config")
    @patch("fraisier.daemon._get_deployer")
    def test_execute_successful_deployment(
        self, mock_get_deployer, mock_get_config, mock_lock
    ):
        """Execute deployment request successfully."""
        # Mock config
        mock_config = MagicMock()
        mock_config.get_fraise_environment.return_value = {
            "type": "api",
            "app_path": "/var/www/api",
        }
        mock_get_config.return_value = mock_config

        # Mock deployer
        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = True
        mock_deployer.execute.return_value = MagicMock(
            success=True,
            status=MagicMock(value="success"),
            new_version="abc123",
            duration_seconds=30.0,
        )
        mock_get_deployer.return_value = mock_deployer

        request = DeploymentRequest(
            version=1,
            project="api",
            environment="development",
            branch="dev",
            timestamp="2026-04-02T11:15:23Z",
            triggered_by="webhook",
            options={"force": False},
            metadata={},
        )

        result = execute_deployment_request(request)

        assert result.success is True
        assert result.status == "success"
        mock_deployer.execute.assert_called_once()

    @patch("fraisier.daemon.get_config")
    def test_execute_unknown_project(self, mock_get_config):
        """Execute deployment for unknown project fails."""
        mock_config = MagicMock()
        mock_config.get_fraise_environment.return_value = None
        mock_get_config.return_value = mock_config

        request = DeploymentRequest(
            version=1,
            project="unknown",
            environment="development",
            branch="dev",
            timestamp="2026-04-02T11:15:23Z",
            triggered_by="webhook",
            options={},
            metadata={},
        )

        result = execute_deployment_request(request)
        assert result.success is False
        assert "not found" in result.error_message

    @patch("fraisier.locking.deployment_lock")
    @patch("fraisier.daemon.get_config")
    @patch("fraisier.daemon._get_deployer")
    def test_execute_force_deployment(
        self, mock_get_deployer, mock_get_config, mock_lock
    ):
        """Execute deployment when forced even if not needed."""
        # Mock config
        mock_config = MagicMock()
        mock_config.get_fraise_environment.return_value = {
            "type": "api",
            "app_path": "/var/www/api",
        }
        mock_get_config.return_value = mock_config

        # Mock deployer
        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = False  # Not needed
        mock_deployer.execute.return_value = MagicMock(
            success=True,
            status=MagicMock(value="success"),
            new_version="abc123",
            duration_seconds=30.0,
        )
        mock_get_deployer.return_value = mock_deployer

        request = DeploymentRequest(
            version=1,
            project="api",
            environment="development",
            branch="dev",
            timestamp="2026-04-02T11:15:23Z",
            triggered_by="webhook",
            options={"force": True},  # Forced
            metadata={},
        )

        result = execute_deployment_request(request)

        assert result.success is True
        mock_deployer.execute.assert_called_once()


class TestDeployDaemonCommand:
    """Tests for the deploy-daemon CLI command."""

    @patch("fraisier.daemon.execute_deployment_request")
    @patch("fraisier.daemon.parse_deployment_request")
    def test_deploy_daemon_success(self, mock_parse, mock_execute, runner):
        """deploy-daemon executes successfully."""
        # Mock parsing
        mock_request = MagicMock()
        mock_request.project = "api"
        mock_parse.return_value = mock_request

        # Mock execution
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.message = "Deployment completed"
        mock_result.deployed_version = "abc123"
        mock_execute.return_value = mock_result

        # Test with stdin input
        json_input = '{"version": 1, "project": "api", "environment": "dev"}'
        result = runner.invoke(
            main, ["deploy-daemon", "--project", "api"], input=json_input
        )

        assert result.exit_code == 0
        assert "Deployment successful" in result.output
        assert "Version: abc123" in result.output
        mock_parse.assert_called_once_with(json_input)
        mock_execute.assert_called_once_with(mock_request)

    @patch("fraisier.daemon.parse_deployment_request")
    def test_deploy_daemon_invalid_json(self, mock_parse, runner):
        """deploy-daemon handles invalid JSON."""
        mock_parse.side_effect = ValueError("Invalid JSON")

        json_input = "invalid json"
        result = runner.invoke(
            main, ["deploy-daemon", "--project", "api"], input=json_input
        )

        assert result.exit_code == 1
        assert "Error parsing request" in result.output

    @patch("fraisier.daemon.parse_deployment_request")
    def test_deploy_daemon_project_mismatch(self, mock_parse, runner):
        """deploy-daemon rejects project mismatch."""
        mock_request = MagicMock()
        mock_request.project = "other_project"
        mock_parse.return_value = mock_request

        json_input = '{"version": 1, "project": "other_project"}'
        result = runner.invoke(
            main, ["deploy-daemon", "--project", "api"], input=json_input
        )

        assert result.exit_code == 1
        assert "Project mismatch" in result.output

    @patch("fraisier.daemon.execute_deployment_request")
    @patch("fraisier.daemon.parse_deployment_request")
    def test_deploy_daemon_execution_failure(self, mock_parse, mock_execute, runner):
        """deploy-daemon handles execution failure."""
        # Mock parsing
        mock_request = MagicMock()
        mock_request.project = "api"  # Set project to match daemon config
        mock_parse.return_value = mock_request

        # Mock execution failure
        mock_execute.return_value = MagicMock(
            success=False, error_message="Deployment failed"
        )

        json_input = '{"version": 1, "project": "api"}'
        result = runner.invoke(
            main, ["deploy-daemon", "--project", "api"], input=json_input
        )

        assert result.exit_code == 1
        assert "Deployment failed" in result.output

    def test_execute_deployment_request_dry_run_no_changes(self):
        """execute_deployment_request handles dry-run when no changes needed."""
        from unittest.mock import MagicMock, patch

        from fraisier.daemon import DeploymentRequest, execute_deployment_request

        request = DeploymentRequest(
            version=1,
            project="test_project",
            environment="development",
            branch="main",
            timestamp="2026-04-02T12:00:00Z",
            triggered_by="cli",
            options={"dry_run": True, "force": False},
            metadata={},
        )

        # Mock config and deployer
        with (
            patch("fraisier.daemon.get_config") as mock_config,
            patch("fraisier.daemon._get_deployer") as mock_get_deployer,
        ):
            mock_config_instance = MagicMock()
            mock_config_instance.get_fraise_environment.return_value = {
                "type": "api",
                "app_path": "/opt/test",
            }
            mock_config.return_value = mock_config_instance

            mock_deployer = MagicMock()
            mock_deployer.is_deployment_needed.return_value = False
            mock_deployer.get_current_version.return_value = "abc123"
            mock_deployer.get_latest_version.return_value = "abc123"
            mock_get_deployer.return_value = mock_deployer

            result = execute_deployment_request(request)

            assert result.success is True
            assert result.status == "dry_run_no_changes"
            assert "Already up to date" in result.message
            assert result.deployed_version == "abc123"

    def test_execute_deployment_request_dry_run_with_changes(self):
        """execute_deployment_request handles dry-run when changes are needed."""
        from unittest.mock import MagicMock, patch

        from fraisier.daemon import DeploymentRequest, execute_deployment_request

        request = DeploymentRequest(
            version=1,
            project="test_project",
            environment="development",
            branch="main",
            timestamp="2026-04-02T12:00:00Z",
            triggered_by="cli",
            options={"dry_run": True, "force": False},
            metadata={},
        )

        # Mock config and deployer
        with (
            patch("fraisier.daemon.get_config") as mock_config,
            patch("fraisier.daemon._get_deployer") as mock_get_deployer,
        ):
            mock_config_instance = MagicMock()
            mock_config_instance.get_fraise_environment.return_value = {
                "type": "api",
                "app_path": "/opt/test",
            }
            mock_config.return_value = mock_config_instance

            mock_deployer = MagicMock()
            mock_deployer.is_deployment_needed.return_value = True
            mock_deployer.get_current_version.return_value = "abc123"
            mock_deployer.get_latest_version.return_value = "def456"
            mock_get_deployer.return_value = mock_deployer

            result = execute_deployment_request(request)

            assert result.success is True
            assert result.status == "dry_run_plan"
            assert "Would deploy abc123 -> def456" in result.message

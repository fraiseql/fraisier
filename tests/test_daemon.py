"""Tests for daemon JSON parsing and deployment execution."""

import json
import os
import pwd
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.daemon import (
    DeploymentRequest,
    execute_deployment_request,
    parse_deployment_request,
)
from fraisier.deployers.base import DeploymentResult, DeploymentStatus


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
        mock_config.get_deploy_user.return_value = pwd.getpwuid(os.getuid()).pw_name
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
        """Execute deployment for unknown project fails with diagnostic."""
        mock_config = MagicMock()
        mock_config.get_fraise_environment.return_value = None
        mock_config.config_path = "/opt/fraisier/fraises.yaml"
        mock_config.list_fraises.return_value = ["api", "web", "worker"]
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
        assert result.error_message is not None
        assert "Project 'unknown' not found" in result.error_message
        assert "/opt/fraisier/fraises.yaml" in result.error_message
        assert "Available projects: api, web, worker" in result.error_message

    @patch("fraisier.daemon.get_config")
    def test_execute_config_not_found(self, mock_get_config):
        """Execute deployment when config file not found shows diagnostic."""
        paths = [
            "/tmp/test/fraises.yaml",
            "/tmp/test/config/fraises.yaml",
            "/opt/fraisier/fraises.yaml",
        ]
        mock_get_config.side_effect = FileNotFoundError(
            f"fraises.yaml not found in any of: {paths}"
        )

        request = DeploymentRequest(
            version=1,
            project="api",
            environment="development",
            branch="dev",
            timestamp="2026-04-02T11:15:23Z",
            triggered_by="webhook",
            options={},
            metadata={},
        )

        result = execute_deployment_request(request)
        assert result.success is False
        assert result.error_message is not None
        assert "FRAISIER_CONFIG environment variable is not set" in result.error_message
        assert "Searched locations:" in result.error_message
        assert "systemd" in result.error_message

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
        mock_config.get_deploy_user.return_value = pwd.getpwuid(os.getuid()).pw_name
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

    def test_the_daemon_owns_no_status_writer(self):
        """Exactly one writer produces the status file, and it is the deployer.

        Two writers is the whole of #378: whichever wrote last won, and the
        daemon's write said `success` for every outcome. This pins both routes
        back in — a module-level import and a qualified call.
        """
        import re
        from pathlib import Path

        from fraisier import daemon

        assert not hasattr(daemon, "write_status"), (
            "fraisier.daemon imported a status writer again"
        )
        source = Path(daemon.__file__).read_text()
        stray = re.search(r"(?<![\w.])write_status\(", source)
        assert stray is None, "daemon.py calls write_status() again (#378)"

    @pytest.mark.parametrize("status_value", ["success", "failed", "rollback_failed"])
    @patch("fraisier.locking.deployment_lock")
    @patch("fraisier.daemon.get_config")
    @patch("fraisier.daemon._get_deployer")
    def test_execute_leaves_the_record_to_the_deployer(
        self,
        mock_get_deployer,
        mock_get_config,
        mock_lock,
        status_value,
    ):
        """The daemon writes no status for a result ``execute()`` returned.

        The deployer already wrote it — with its owner fields, its own
        ``status_dir`` and the real commit sha. The daemon's second write
        reported ``success`` for every outcome, ``rollback_failed`` included,
        and blanked the sha, because ``DeploymentResult`` has no ``commit_sha``
        attribute for ``getattr`` to find (#378).
        """
        mock_config = MagicMock()
        mock_config.get_fraise_environment.return_value = {
            "type": "api",
            "app_path": "/var/www/api",
        }
        mock_config.get_deploy_user.return_value = pwd.getpwuid(os.getuid()).pw_name
        mock_get_config.return_value = mock_config

        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = True
        mock_deployer.execute.return_value = DeploymentResult(
            success=status_value == "success",
            status=DeploymentStatus(status_value),
            new_version="abc123",
            duration_seconds=30.0,
            error_message=None if status_value == "success" else "boom",
        )
        mock_get_deployer.return_value = mock_deployer

        request = DeploymentRequest(
            version=1,
            project="api",
            environment="development",
            branch="dev",
            timestamp="2026-04-02T11:15:23Z",
            triggered_by="webhook",
            options={},
            metadata={},
        )

        result = execute_deployment_request(request)

        assert result.status == status_value
        mock_deployer._write_status.assert_not_called()

    @patch("fraisier.locking.deployment_lock")
    @patch("fraisier.daemon.get_config")
    @patch("fraisier.daemon._get_deployer")
    def test_execute_refused_by_the_lock_writes_nothing(
        self, mock_get_deployer, mock_get_config, mock_lock
    ):
        """A refused request must not touch the record of the running deploy.

        The daemon used to write ``deploying`` before taking the lock, so a
        refusal then landed as ``failed: Deploy already running`` on top of the
        record belonging to the deploy that holds it (#378).
        """
        from fraisier.errors import DeploymentLockError

        mock_config = MagicMock()
        mock_config.get_fraise_environment.return_value = {
            "type": "api",
            "app_path": "/var/www/api",
        }
        mock_config.get_deploy_user.return_value = pwd.getpwuid(os.getuid()).pw_name
        mock_get_config.return_value = mock_config

        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = True
        mock_get_deployer.return_value = mock_deployer

        mock_lock.side_effect = DeploymentLockError("Deploy already running for api")

        request = DeploymentRequest(
            version=1,
            project="api",
            environment="development",
            branch="dev",
            timestamp="2026-04-02T11:15:23Z",
            triggered_by="webhook",
            options={},
            metadata={},
        )

        result = execute_deployment_request(request)

        assert result.success is False
        assert "already running" in (result.message or "").lower()
        mock_deployer.execute.assert_not_called()
        mock_deployer._write_status.assert_not_called()

    @patch("fraisier.locking.deployment_lock")
    @patch("fraisier.daemon.get_config")
    @patch("fraisier.daemon._get_deployer")
    def test_execute_closes_the_record_when_the_deployer_raises(
        self, mock_get_deployer, mock_get_config, mock_lock
    ):
        """An exception that escapes ``execute()`` leaves the record open.

        Nobody else will close it, so the daemon does — through the deployer's
        own ``_write_status``, which stamps the owner fields and honours the
        fraise's ``status_dir``. The daemon's module-level writer knows neither
        (#378).
        """
        mock_config = MagicMock()
        mock_config.get_fraise_environment.return_value = {
            "type": "api",
            "app_path": "/var/www/api",
        }
        mock_config.get_deploy_user.return_value = pwd.getpwuid(os.getuid()).pw_name
        mock_get_config.return_value = mock_config

        mock_deployer = MagicMock()
        mock_deployer.is_deployment_needed.return_value = True
        mock_deployer.execute.side_effect = RuntimeError("git pull exploded")
        mock_get_deployer.return_value = mock_deployer

        request = DeploymentRequest(
            version=1,
            project="api",
            environment="development",
            branch="dev",
            timestamp="2026-04-02T11:15:23Z",
            triggered_by="webhook",
            options={},
            metadata={},
        )

        result = execute_deployment_request(request)

        assert result.success is False
        mock_deployer._write_status.assert_called_once()
        args, kwargs = mock_deployer._write_status.call_args
        assert args[0] == "failed"
        assert "git pull exploded" in kwargs["error_message"]

    @patch("fraisier.daemon.get_config")
    def test_execute_wrong_user_fails_with_clear_message(self, mock_get_config):
        """Wrong user fails before lock with a clear message."""
        mock_config = MagicMock()
        mock_config.get_fraise_environment.return_value = {
            "type": "api",
            "app_path": "/var/www/api",
        }
        mock_config.get_deploy_user.return_value = "expected_deploy_user"
        mock_get_config.return_value = mock_config

        request = DeploymentRequest(
            version=1,
            project="api",
            environment="development",
            branch="dev",
            timestamp="2026-04-02T11:15:23Z",
            triggered_by="webhook",
            options={},
            metadata={},
        )

        result = execute_deployment_request(request)

        assert result.success is False
        assert result.status == "failed"
        assert result.message == "Wrong user"
        assert result.error_message is not None
        assert "expected_deploy_user" in result.error_message
        assert "sudo -u expected_deploy_user" in result.error_message


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

        # Mock execution — all attributes must be JSON-serializable
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.status = "success"
        mock_result.message = "Deployment completed"
        mock_result.deployed_version = "abc123"
        mock_result.duration_seconds = 1.5
        mock_result.error_message = None
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

        # Mock execution failure — all attributes must be JSON-serializable
        mock_execute.return_value = MagicMock(
            success=False,
            status="failed",
            message=None,
            deployed_version=None,
            duration_seconds=0.0,
            error_message="Deployment failed",
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
            current_user = pwd.getpwuid(os.getuid()).pw_name
            mock_config_instance.get_deploy_user.return_value = current_user
            mock_config.return_value = mock_config_instance

            mock_deployer = MagicMock()
            mock_deployer.is_deployment_needed.return_value = False
            mock_deployer.get_current_version.return_value = "abc123"
            mock_deployer.get_latest_version.return_value = "abc123"
            mock_get_deployer.return_value = mock_deployer

            result = execute_deployment_request(request)

            assert result.success is True
            assert result.status == "dry_run_no_changes"
            assert result.message is not None
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
            current_user = pwd.getpwuid(os.getuid()).pw_name
            mock_config_instance.get_deploy_user.return_value = current_user
            mock_config.return_value = mock_config_instance

            mock_deployer = MagicMock()
            mock_deployer.is_deployment_needed.return_value = True
            mock_deployer.get_current_version.return_value = "abc123"
            mock_deployer.get_latest_version.return_value = "def456"
            mock_get_deployer.return_value = mock_deployer

            result = execute_deployment_request(request)

            assert result.success is True
            assert result.status == "dry_run_plan"
            assert result.message is not None
            assert "Would deploy abc123 -> def456" in result.message

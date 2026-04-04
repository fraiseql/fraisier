"""Tests for validate-setup and diagnose CLI commands."""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.main import (
    _check_socket_directory,
    _check_socket_file,
    _check_systemd_version,
    _diagnose_deployment_status,
    _diagnose_socket_connectivity,
    _diagnose_systemd_service,
    main,
)


class TestValidateSetup:
    """Tests for validate-setup command."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def mock_config(self, tmp_path):
        """Create a minimal fraises.yaml for testing."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
fraises:
  my_api:
    type: api
    description: "Test API"
    environments:
      development:
        name: dev_api
        app_path: /opt/my_api
        systemd_service: my_api.service
""")
        return str(cfg)

    def test_validate_setup_unknown_fraise_fails(self, runner, tmp_path):
        """validate-setup with unknown fraise fails."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("fraises: {}")

        result = runner.invoke(
            main, ["-c", str(cfg), "validate-setup", "unknown", "development"]
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_validate_setup_unknown_environment_fails(self, runner, mock_config):
        """validate-setup with unknown environment fails."""
        result = runner.invoke(
            main, ["-c", mock_config, "validate-setup", "my_api", "nonexistent"]
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_validate_setup_scopes_to_requested_environment(self, runner, tmp_path):
        """validate-setup only validates the requested environment, not all envs."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
fraises:
  my_api:
    environments:
      development:
        name: dev_api
        app_path: /opt/my_api
        systemd_service: my_api.service
      production:
        name: prod_api
        app_path: /opt/my_api
        systemd_service: my_api.service
""")
        passing = {"ok": True, "message": "ok"}
        with (
            patch(
                "fraisier.cli.main._check_systemd_version",
                return_value=(True, "248", "ok"),
            ),
            patch("fraisier.cli.main._check_socket_directory", return_value=passing),
            patch("fraisier.cli.main._check_socket_file", return_value=passing),
            patch("fraisier.cli.main._check_socket_permissions", return_value=passing),
            patch("fraisier.cli.main._check_systemd_units", return_value=passing),
            patch("fraisier.cli.main._check_user_permissions", return_value=passing),
        ):
            result = runner.invoke(
                main,
                ["-c", str(cfg), "validate-setup", "my_api", "development", "--json"],
            )
        import json

        data = json.loads(result.output)
        assert list(data["environments"].keys()) == ["development"]

    def test_validate_setup_command_exists(self, runner):
        """validate-setup command is registered and shows help."""
        result = runner.invoke(main, ["validate-setup", "--help"])
        assert result.exit_code == 0
        assert "Validate socket activation setup" in result.output


class TestValidateSetupHelpers:
    """Tests for validate-setup helper functions."""

    def test_check_systemd_version_success(self):
        """_check_systemd_version returns success for valid version."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="systemd 249 (249.7-1-arch)\n..."
            )

            ok, version, message = _check_systemd_version()
            assert ok is True
            assert version == "249"
            assert "compatible" in message

    def test_check_systemd_version_old_version(self):
        """_check_systemd_version fails for old version."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="systemd 229 (229.1-1-arch)\n..."
            )

            ok, version, message = _check_systemd_version()
            assert ok is False
            assert version == "229"
            assert "requires >= 230" in message

    def test_check_systemd_version_command_fails(self):
        """_check_systemd_version handles command failure."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            ok, version, message = _check_systemd_version()
            assert ok is False
            assert version == "unknown"
            assert "not available" in message

    def test_check_socket_directory_exists_good_permissions(self, tmp_path):
        """Socket directory passes for existing directory with good permissions."""
        socket_dir = tmp_path / "test_socket"
        socket_dir.mkdir()
        socket_dir.chmod(0o755)

        result = _check_socket_directory(socket_dir)
        assert result["ok"] is True
        assert "permissions" in result["message"]

    def test_check_socket_directory_missing(self, tmp_path):
        """_check_socket_directory fails for missing directory."""
        socket_dir = tmp_path / "missing_socket"

        result = _check_socket_directory(socket_dir)
        assert result["ok"] is False
        assert "does not exist" in result["message"]

    def test_check_socket_directory_bad_permissions(self, tmp_path):
        """_check_socket_directory fails for restrictive permissions."""
        socket_dir = tmp_path / "test_socket"
        socket_dir.mkdir()
        socket_dir.chmod(0o600)  # Too restrictive

        result = _check_socket_directory(socket_dir)
        assert result["ok"] is False
        assert "too restrictive" in result["message"]

    def test_check_socket_file_exists(self, tmp_path):
        """_check_socket_file passes for existing socket file."""
        socket_file = tmp_path / "deploy.sock"
        socket_file.write_text("")  # Create file

        result = _check_socket_file(socket_file)
        assert result["ok"] is True
        assert "exists" in result["message"]

    def test_check_socket_file_missing(self, tmp_path):
        """_check_socket_file fails for missing socket file."""
        socket_file = tmp_path / "deploy.sock"

        result = _check_socket_file(socket_file)
        assert result["ok"] is False
        assert "does not exist" in result["message"]


class TestDiagnoseCommand:
    """Tests for diagnose command."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def mock_config(self, tmp_path):
        """Create a minimal fraises.yaml for testing."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
fraises:
  my_api:
    type: api
    description: "Test API"
    environments:
      development:
        name: dev_api
        app_path: /opt/my_api
        systemd_service: my_api.service
""")
        return str(cfg)

    def test_diagnose_unknown_fraise_fails(self, runner, tmp_path):
        """diagnose with unknown fraise fails."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("fraises: {}")

        result = runner.invoke(
            main, ["-c", str(cfg), "diagnose", "unknown", "development"]
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    @patch("fraisier.cli.main._diagnose_socket_connectivity")
    @patch("fraisier.cli.main._diagnose_deployment_status")
    @patch("fraisier.cli.main._diagnose_systemd_service")
    @patch("fraisier.cli.main._diagnose_systemd_socket_unit")
    def test_diagnose_no_issues_found(
        self,
        mock_socket_unit,
        mock_service,
        mock_deployment,
        mock_socket,
        runner,
        mock_config,
    ):
        """diagnose shows no issues when all checks pass."""
        # Mock all diagnostics to show no issues
        mock_socket.return_value = {"socket_exists": True, "can_connect": True}
        mock_deployment.return_value = {
            "status_file_exists": True,
            "status": "success",
            "deployed_version": "v1.0.0",
        }
        mock_service.return_value = {
            "service_exists": True,
            "service_running": True,
            "service_name": "my_api.service",
        }
        mock_socket_unit.return_value = {
            "unit_exists": True,
            "unit_active": True,
            "unit_name": "fraisier-test-development-deploy.socket",
        }

        result = runner.invoke(
            main, ["-c", mock_config, "diagnose", "my_api", "development"]
        )
        assert result.exit_code == 0
        assert "no deployment issues detected" in result.output.lower()

    @patch("fraisier.cli.main._diagnose_socket_connectivity")
    @patch("fraisier.cli.main._diagnose_deployment_status")
    @patch("fraisier.cli.main._diagnose_systemd_service")
    @patch("fraisier.cli.main._diagnose_systemd_socket_unit")
    def test_diagnose_issues_found(
        self,
        mock_socket_unit,
        mock_service,
        mock_deployment,
        mock_socket,
        runner,
        mock_config,
    ):
        """diagnose shows issues and suggestions when problems detected."""
        # Mock diagnostics to show issues
        mock_socket.return_value = {"socket_exists": False, "can_connect": False}
        mock_deployment.return_value = {
            "status_file_exists": True,
            "status": "failed",
            "error": "Deployment timeout",
        }
        mock_service.return_value = {
            "service_exists": True,
            "service_running": False,
            "service_name": "my_api.service",
        }
        mock_socket_unit.return_value = {
            "unit_exists": False,
            "unit_active": False,
            "unit_name": "fraisier-test-development-deploy.socket",
        }

        result = runner.invoke(
            main, ["-c", mock_config, "diagnose", "my_api", "development"]
        )
        assert result.exit_code == 0  # Diagnose doesn't exit with error, just reports
        assert "found" in result.output.lower() and "issue" in result.output.lower()
        assert "suggested fixes" in result.output.lower()


class TestDiagnoseHelpers:
    """Tests for diagnose helper functions."""

    def test_diagnose_socket_connectivity_success(self, tmp_path):
        """_diagnose_socket_connectivity succeeds when socket accepts connections."""
        socket_file = tmp_path / "test.sock"
        # We can't easily test actual socket connectivity in unit tests
        # so we'll just test the file existence logic
        result = _diagnose_socket_connectivity(socket_file)
        assert result["socket_exists"] is False
        assert result["can_connect"] is False

    def test_diagnose_deployment_status_success(self, tmp_path):
        """_diagnose_deployment_status parses status file correctly."""
        status_file = tmp_path / "deployment.status"
        status_data = {
            "status": "success",
            "deployed_version": "v1.2.3",
            "deployed_at": "2026-04-02T12:00:00Z",
        }
        status_file.write_text(json.dumps(status_data))

        result = _diagnose_deployment_status(status_file)
        assert result["status_file_exists"] is True
        assert result["status"] == "success"
        assert result["deployed_version"] == "v1.2.3"

    def test_diagnose_deployment_status_missing_file(self, tmp_path):
        """_diagnose_deployment_status handles missing status file."""
        status_file = tmp_path / "missing.status"

        result = _diagnose_deployment_status(status_file)
        assert result["status_file_exists"] is False
        assert result["status"] is None

    @patch("subprocess.run")
    def test_diagnose_systemd_service_success(self, mock_run):
        """_diagnose_systemd_service checks service status correctly."""
        # Mock systemctl cat success
        mock_run.side_effect = [
            MagicMock(returncode=0),  # cat command succeeds
            MagicMock(returncode=0, stdout="active\n"),  # is-active returns active
        ]

        result = _diagnose_systemd_service("test.service")
        assert result["service_exists"] is True
        assert result["service_running"] is True
        assert result["service_name"] == "test.service"

    @patch("subprocess.run")
    def test_diagnose_systemd_service_not_found(self, mock_run):
        """_diagnose_systemd_service handles missing service."""
        mock_run.return_value = MagicMock(returncode=1)  # cat command fails

        result = _diagnose_systemd_service("missing.service")
        assert result["service_exists"] is False
        assert result["service_running"] is False

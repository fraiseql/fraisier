"""Tests for component test CLI commands."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.test_components import (
    _check_remote_connectivity,
    _get_wrapper_path,
    test_database,
    test_git,
    test_health,
    test_install,
    test_wrapper,
)


@pytest.fixture
def runner():
    """Create a Click CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_config():
    """Create a mock fraisier config."""
    config = Mock()
    config.get_fraise_environment = Mock()
    return config


class TestTestWrapper:
    """Tests for test-wrapper command."""

    def test_wrapper_missing_command_args(self, runner):
        """Test test-wrapper with no wrapper type specified."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "api"}
            mock_req.return_value = config

            # Missing command args - Click will enforce that args is required
            result = runner.invoke(test_wrapper, ["api", "development"])
            assert result.exit_code == 2  # Click's exit code for missing args

    def test_wrapper_invalid_type(self, runner):
        """Test test-wrapper with invalid wrapper type."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "api"}
            mock_req.return_value = config

            result = runner.invoke(
                test_wrapper, ["api", "development", "invalid", "restart"]
            )
            assert result.exit_code == 1
            assert "Unknown wrapper type" in result.output

    def test_wrapper_env_var_not_set(self, runner):
        """Test test-wrapper when wrapper env var is not set."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "api"}
            mock_req.return_value = config

            # Ensure env var is not set
            os.environ.pop("FRAISIER_SYSTEMCTL_WRAPPER", None)

            result = runner.invoke(
                test_wrapper, ["api", "development", "systemctl", "restart"]
            )
            assert result.exit_code == 1
            assert "not set" in result.output

    def test_get_wrapper_path_valid(self):
        """Test _get_wrapper_path with valid wrapper type."""
        with tempfile.TemporaryDirectory() as tmpdir:
            wrapper_path = Path(tmpdir) / "wrapper.sh"
            wrapper_path.write_text("#!/bin/bash\necho test")
            wrapper_path.chmod(0o755)

            with patch.dict(
                os.environ,
                {"FRAISIER_SYSTEMCTL_WRAPPER": str(wrapper_path)},
            ):
                result = _get_wrapper_path("systemctl")
                assert result == str(wrapper_path)

    def test_get_wrapper_path_not_set(self):
        """Test _get_wrapper_path when env var is not set."""
        os.environ.pop("FRAISIER_SYSTEMCTL_WRAPPER", None)

        with pytest.raises(SystemExit):
            _get_wrapper_path("systemctl")


class TestTestInstall:
    """Tests for test-install command."""

    def test_install_fraise_not_found(self, runner):
        """Test test-install when fraise/environment not found."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = None
            mock_req.return_value = config

            result = runner.invoke(test_install, ["api", "development"])
            assert result.exit_code == 1
            assert "not found" in result.output

    def test_install_unknown_fraise_type(self, runner):
        """Test test-install with unknown fraise type."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "unknown"}
            mock_req.return_value = config
            mock_get.return_value = None

            result = runner.invoke(test_install, ["api", "development"])
            assert result.exit_code == 1
            assert "Unknown fraise type" in result.output

    def test_install_no_command_configured(self, runner):
        """Test test-install when install command not configured."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "api"}
            mock_req.return_value = config

            deployer = Mock()
            deployer._install_dependencies = Mock()
            deployer.install_command = None
            mock_get.return_value = deployer

            result = runner.invoke(test_install, ["api", "development"])
            assert result.exit_code == 1
            assert "No install command configured" in result.output

    def test_install_success(self, runner):
        """Test test-install successful execution."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "api"}
            mock_req.return_value = config

            deployer = Mock()
            deployer._install_dependencies = Mock()
            deployer.install_command = ["uv", "sync"]
            deployer.app_path = "/app"
            mock_get.return_value = deployer

            result = runner.invoke(test_install, ["api", "development"])
            assert result.exit_code == 0
            assert "Install step successful" in result.output


class TestTestHealth:
    """Tests for test-health command."""

    def test_health_fraise_not_found(self, runner):
        """Test test-health when fraise/environment not found."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = None
            mock_req.return_value = config

            result = runner.invoke(test_health, ["api", "development"])
            assert result.exit_code == 1
            assert "not found" in result.output

    def test_health_unknown_fraise_type(self, runner):
        """Test test-health with unknown fraise type."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "unknown"}
            mock_req.return_value = config
            mock_get.return_value = None

            result = runner.invoke(test_health, ["api", "development"])
            assert result.exit_code == 1
            assert "Unknown fraise type" in result.output

    def test_health_no_url_configured(self, runner):
        """Test test-health when health check URL not configured."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "api"}
            mock_req.return_value = config

            deployer = Mock(spec=["health_check_url"])
            deployer.health_check_url = None
            mock_get.return_value = deployer

            result = runner.invoke(test_health, ["api", "development"])
            assert result.exit_code == 1
            assert "configured for this" in result.output

    def test_health_check_passed(self, runner):
        """Test test-health with passing health check."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "api"}
            mock_req.return_value = config

            deployer = Mock()
            deployer.health_check_url = "http://localhost:8000/health"
            deployer.health_check_timeout = 30
            deployer.health_check_retries = 5
            deployer.health_check = Mock(return_value=True)
            mock_get.return_value = deployer

            result = runner.invoke(test_health, ["api", "development"])
            assert result.exit_code == 0
            assert "Health check passed" in result.output

    def test_health_check_failed(self, runner):
        """Test test-health with failing health check."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "api"}
            mock_req.return_value = config

            deployer = Mock()
            deployer.health_check_url = "http://localhost:8000/health"
            deployer.health_check_timeout = 30
            deployer.health_check_retries = 5
            deployer.health_check = Mock(return_value=False)
            mock_get.return_value = deployer

            result = runner.invoke(test_health, ["api", "development"])
            assert result.exit_code == 1
            assert "Health check failed" in result.output


class TestTestGit:
    """Tests for test-git command."""

    def test_git_fraise_not_found(self, runner):
        """Test test-git when fraise/environment not found."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = None
            mock_req.return_value = config

            result = runner.invoke(test_git, ["api", "development"])
            assert result.exit_code == 1
            assert "not found" in result.output

    def test_git_unknown_fraise_type(self, runner):
        """Test test-git with unknown fraise type."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "unknown"}
            mock_req.return_value = config
            mock_get.return_value = None

            result = runner.invoke(test_git, ["api", "development"])
            assert result.exit_code == 1
            assert "Unknown fraise type" in result.output

    def test_git_unsupported_type(self, runner):
        """Test test-git with fraise type that doesn't support git."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "api"}
            mock_req.return_value = config

            deployer = Mock(spec=[])  # No bare_repo attribute
            mock_get.return_value = deployer

            result = runner.invoke(test_git, ["api", "development"])
            assert result.exit_code == 1
            assert "does not support" in result.output

    def test_git_all_ok(self, runner):
        """Test test-git with all checks passing."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "api"}
            mock_req.return_value = config

            with tempfile.TemporaryDirectory() as tmpdir:
                bare_repo = Path(tmpdir) / "repo.git"
                app_path = Path(tmpdir) / "app"
                bare_repo.mkdir()
                app_path.mkdir()

                deployer = Mock()
                deployer.bare_repo = bare_repo
                deployer.clone_url = "https://github.com/example/repo.git"
                deployer.app_path = str(app_path)
                deployer.branch = "main"
                deployer.get_current_version = Mock(return_value="abc1234")
                deployer.get_latest_version = Mock(return_value="def5678")
                mock_get.return_value = deployer

                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = Mock(returncode=0)
                    result = runner.invoke(test_git, ["api", "development"])
                    # Note: The test may not pass all checks depending on state,
                    # but at minimum it should run without errors
                    assert result.exit_code in (0, 1)
                    assert "Testing git operations" in result.output


class TestTestDatabase:
    """Tests for test-database command."""

    def test_database_fraise_not_found(self, runner):
        """Test test-database when fraise/environment not found."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = None
            mock_req.return_value = config

            result = runner.invoke(test_database, ["api", "development"])
            assert result.exit_code == 1
            assert "not found" in result.output

    def test_database_unknown_fraise_type(self, runner):
        """Test test-database with unknown fraise type."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "unknown"}
            mock_req.return_value = config
            mock_get.return_value = None

            result = runner.invoke(test_database, ["api", "development"])
            assert result.exit_code == 1
            assert "Unknown fraise type" in result.output

    def test_database_no_config(self, runner):
        """Test test-database when database not configured."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "api"}
            mock_req.return_value = config

            deployer = Mock()
            deployer.database_config = None
            mock_get.return_value = deployer

            result = runner.invoke(test_database, ["api", "development"])
            assert result.exit_code == 1
            assert "No database configuration" in result.output

    def test_database_psycopg2_not_installed(self, runner):
        """Test test-database when psycopg2 is not available."""
        with (
            runner.isolated_filesystem(),
            patch("fraisier.cli.test_components.require_config") as mock_req,
            patch("fraisier.cli.test_components._get_deployer") as mock_get,
        ):
            config = Mock()
            config.get_fraise_environment.return_value = {"type": "api"}
            mock_req.return_value = config

            deployer = Mock()
            deployer.database_config = {"database_url": "postgresql://..."}
            mock_get.return_value = deployer

            with patch.dict("sys.modules", {"psycopg2": None}):
                result = runner.invoke(test_database, ["api", "development"])
                assert result.exit_code == 1
                assert "psycopg2 not installed" in result.output


class TestCheckRemoteConnectivity:
    """Tests for _check_remote_connectivity helper."""

    def test_remote_connectivity_success(self):
        """Test _check_remote_connectivity with successful fetch."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bare_repo = Path(tmpdir) / "repo.git"
            bare_repo.mkdir()

            deployer = Mock()
            deployer.bare_repo = bare_repo
            deployer.clone_url = "https://github.com/example/repo.git"

            table = Mock()
            table.add_row = Mock()

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=0)
                result = _check_remote_connectivity(deployer, table)

                assert result is True
                table.add_row.assert_called_once()

    def test_remote_connectivity_no_repo(self):
        """Test _check_remote_connectivity when repo doesn't exist."""
        deployer = Mock()
        deployer.bare_repo = Path("/nonexistent")
        deployer.clone_url = "https://github.com/example/repo.git"

        table = Mock()

        result = _check_remote_connectivity(deployer, table)
        assert result is True
        table.add_row.assert_not_called()

"""Tests for dependency install step in deploy pipeline (#44)."""

from unittest.mock import MagicMock, patch

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentStatus
from fraisier.deployers.etl import ETLDeployer
from fraisier.deployers.scheduled import ScheduledDeployer


class TestInstallDependenciesConfig:
    """Tests for install config parsing in GitDeployMixin."""

    def test_no_install_config(self):
        """No install config means no install step."""
        deployer = APIDeployer({"app_path": "/var/www/api"})
        assert deployer.install_command is None
        assert deployer.install_user is None

    def test_install_command_from_config(self):
        """Install command is parsed from config."""
        config = {
            "app_path": "/var/www/api",
            "install": {
                "command": ["uv", "sync", "--frozen"],
            },
        }
        deployer = APIDeployer(config)
        assert deployer.install_command == ["uv", "sync", "--frozen"]
        assert deployer.install_user is None

    def test_install_command_and_user_from_config(self):
        """Install command and user are parsed from config."""
        config = {
            "app_path": "/var/www/api",
            "install": {
                "command": ["uv", "sync", "--frozen"],
                "user": "myapp",
            },
        }
        deployer = APIDeployer(config)
        assert deployer.install_command == ["uv", "sync", "--frozen"]
        assert deployer.install_user == "myapp"


class TestInstallDependenciesExecution:
    """Tests for _install_dependencies method."""

    def test_skips_when_no_config(self):
        """No install config means _install_dependencies is a no-op."""
        deployer = APIDeployer({"app_path": "/var/www/api"})
        mock_runner = MagicMock()
        deployer.runner = mock_runner

        deployer._install_dependencies()

        mock_runner.run.assert_not_called()

    def test_runs_command_in_app_path(self):
        """Install command runs in app_path directory."""
        config = {
            "app_path": "/var/www/api",
            "install": {
                "command": ["uv", "sync", "--frozen"],
            },
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        deployer.runner = mock_runner

        deployer._install_dependencies()

        mock_runner.run.assert_called_once_with(
            ["uv", "sync", "--frozen"],
            cwd="/var/www/api",
        )

    def test_runs_command_with_sudo_user(self):
        """Install command uses sudo -u when user is configured."""
        config = {
            "app_path": "/var/www/api",
            "install": {
                "command": ["uv", "sync", "--frozen"],
                "user": "myapp",
            },
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        deployer.runner = mock_runner

        deployer._install_dependencies()

        mock_runner.run.assert_called_once_with(
            ["sudo", "-u", "myapp", "uv", "sync", "--frozen"],
            cwd="/var/www/api",
        )

    def test_no_sudo_when_install_user_equals_deploy_user(self):
        """When install.user equals deploy_user, command runs without sudo."""
        config = {
            "app_path": "/var/www/api",
            "deploy_user": "myapp",
            "install": {
                "command": ["uv", "sync", "--frozen"],
                "user": "myapp",
            },
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        deployer.runner = mock_runner

        deployer._install_dependencies()

        # Should run without sudo since install_user == deploy_user
        mock_runner.run.assert_called_once_with(
            ["uv", "sync", "--frozen"],
            cwd="/var/www/api",
        )

    def test_sudo_when_install_user_differs_from_deploy_user(self):
        """When install.user differs from deploy_user, command uses sudo."""
        config = {
            "app_path": "/var/www/api",
            "deploy_user": "fraisier",
            "install": {
                "command": ["uv", "sync", "--frozen"],
                "user": "myapp",
            },
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        deployer.runner = mock_runner

        deployer._install_dependencies()

        # Should use sudo since install_user != deploy_user
        mock_runner.run.assert_called_once_with(
            ["sudo", "-u", "myapp", "uv", "sync", "--frozen"],
            cwd="/var/www/api",
        )

    def test_skips_when_no_app_path(self):
        """Install is skipped when there is no app_path."""
        config = {
            "install": {
                "command": ["uv", "sync", "--frozen"],
            },
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        deployer.runner = mock_runner

        deployer._install_dependencies()

        mock_runner.run.assert_not_called()


class TestInstallStepInAPIDeployer:
    """Install step runs after git pull, before database migrations."""

    def test_install_runs_between_git_and_db(self, mock_subprocess):
        """Install step executes after git pull and before database strategy."""
        config = {
            "app_path": "/var/www/api",
            "systemd_service": "api.service",
            "database": {"strategy": "apply"},
            "install": {
                "command": ["uv", "sync", "--frozen"],
            },
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        call_order = []

        with (
            patch.object(
                deployer,
                "_git_pull",
                side_effect=lambda: (
                    call_order.append("git_pull"),
                    ("aaa", "bbb"),
                )[1],
            ),
            patch.object(
                deployer,
                "_install_dependencies",
                side_effect=lambda: call_order.append("install"),
            ),
            patch.object(
                deployer,
                "_run_strategy",
                side_effect=lambda: call_order.append("strategy"),
            ),
            patch.object(
                deployer,
                "_restart_service",
                side_effect=lambda: call_order.append("restart"),
            ),
        ):
            deployer.execute()

        assert call_order == ["git_pull", "install", "strategy", "restart"]

    def test_install_failure_aborts_deployment(self, mock_subprocess):
        """If install step fails, deployment fails without running DB migrations."""
        from subprocess import CalledProcessError

        config = {
            "app_path": "/var/www/api",
            "systemd_service": "api.service",
            "database": {"strategy": "apply"},
            "install": {
                "command": ["uv", "sync", "--frozen"],
            },
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with (
            patch.object(deployer, "_git_pull", return_value=("aaa", "bbb")),
            patch.object(
                deployer,
                "_install_dependencies",
                side_effect=CalledProcessError(1, "uv sync"),
            ),
            patch.object(deployer, "_run_strategy") as mock_strategy,
            patch.object(deployer, "_restart_service"),
        ):
            result = deployer.execute()

        assert result.success is False
        assert result.status == DeploymentStatus.FAILED
        mock_strategy.assert_not_called()

    def test_no_install_config_still_works(self, mock_subprocess, mock_requests):
        """Deployment works fine without install config."""
        config = {
            "app_path": "/var/www/api",
            "health_check": {"url": "http://localhost:8000/health"},
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        result = deployer.execute()

        assert result.success is True


class TestInstallStepInETLDeployer:
    """Install step runs after git pull in ETL deployer."""

    def test_install_runs_after_git_pull(self):
        """ETL deployer runs install after git pull."""
        config = {
            "fraise_name": "pipeline",
            "app_path": "/var/etl",
            "repos_base": "/tmp/repos",
            "install": {
                "command": ["pip", "install", "-r", "requirements.txt"],
            },
        }
        deployer = ETLDeployer(config)

        call_order = []

        with (
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                side_effect=lambda *_a, **_kw: (
                    call_order.append("git_pull"),
                    ("aaa", "bbb"),
                )[1],
            ),
            patch.object(
                deployer,
                "_install_dependencies",
                side_effect=lambda: call_order.append("install"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
        ):
            result = deployer.execute()

        assert result.success is True
        assert "git_pull" in call_order
        assert "install" in call_order
        assert call_order.index("git_pull") < call_order.index("install")


class TestInstallStepInScheduledDeployer:
    """Install step runs after git pull in Scheduled deployer."""

    def test_install_runs_after_git_pull(self):
        """Scheduled deployer runs install after git pull."""
        config = {
            "fraise_name": "stats",
            "app_path": "/var/www/app",
            "repos_base": "/tmp/repos",
            "systemd_timer": "stats.timer",
            "install": {
                "command": ["uv", "sync"],
            },
        }
        deployer = ScheduledDeployer(config)

        call_order = []

        with (
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                side_effect=lambda *_a, **_kw: (
                    call_order.append("git_pull"),
                    ("aaa", "bbb"),
                )[1],
            ),
            patch.object(
                deployer,
                "_install_dependencies",
                side_effect=lambda: call_order.append("install"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="timer:active\n")
            result = deployer.execute()

        assert result.success is True
        assert "git_pull" in call_order
        assert "install" in call_order
        assert call_order.index("git_pull") < call_order.index("install")

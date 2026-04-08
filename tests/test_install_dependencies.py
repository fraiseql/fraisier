"""Tests for dependency install step in deploy pipeline (#44)."""

from unittest.mock import MagicMock, patch

import pytest

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


class TestInstallViaSocket:
    """Install via Unix socket helper when FRAISIER_INSTALL_SOCKET_* is set."""

    def test_uses_socket_when_env_var_and_path_exist(self, tmp_path):
        """When socket env var is set and path exists, uses socket instead of sudo."""
        socket_file = tmp_path / "install.sock"
        socket_file.touch()

        config = {
            "app_path": "/var/www/api",
            "deploy_user": "fraisier",
            "fraise_name": "api",
            "environment": "production",
            "install": {
                "command": ["uv", "sync", "--frozen"],
                "user": "appuser",
            },
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        deployer.runner = mock_runner

        with (
            patch.dict(
                "os.environ",
                {"FRAISIER_INSTALL_SOCKET_API_PRODUCTION": str(socket_file)},
            ),
            patch.object(deployer, "_install_via_socket") as mock_socket,
        ):
            deployer._install_dependencies()

        mock_socket.assert_called_once_with(
            str(socket_file), ["uv", "sync", "--frozen"], "/var/www/api"
        )
        mock_runner.run.assert_not_called()

    def test_falls_back_to_sudo_when_socket_missing(self, tmp_path):
        """Falls back to sudo -u when socket file does not exist."""
        config = {
            "app_path": "/var/www/api",
            "deploy_user": "fraisier",
            "fraise_name": "api",
            "environment": "production",
            "install": {
                "command": ["uv", "sync", "--frozen"],
                "user": "appuser",
            },
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        deployer.runner = mock_runner

        with patch.dict(
            "os.environ",
            {"FRAISIER_INSTALL_SOCKET_API_PRODUCTION": str(tmp_path / "missing.sock")},
        ):
            deployer._install_dependencies()

        mock_runner.run.assert_called_once_with(
            ["sudo", "-u", "appuser", "uv", "sync", "--frozen"],
            cwd="/var/www/api",
        )

    def test_falls_back_to_sudo_when_env_var_not_set(self):
        """Falls back to sudo -u when no socket env var is set."""
        config = {
            "app_path": "/var/www/api",
            "deploy_user": "fraisier",
            "fraise_name": "api",
            "environment": "production",
            "install": {
                "command": ["uv", "sync", "--frozen"],
                "user": "appuser",
            },
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        deployer.runner = mock_runner

        with patch.dict("os.environ", {}, clear=True):
            deployer._install_dependencies()

        mock_runner.run.assert_called_once_with(
            ["sudo", "-u", "appuser", "uv", "sync", "--frozen"],
            cwd="/var/www/api",
        )

    def test_hyphenated_names_normalised_to_underscores(self, tmp_path):
        """Hyphens in fraise/env names become underscores in env var key."""
        socket_file = tmp_path / "install.sock"
        socket_file.touch()

        config = {
            "app_path": "/var/www/api",
            "deploy_user": "fraisier",
            "fraise_name": "my-api",
            "environment": "pre-production",
            "install": {
                "command": ["uv", "sync", "--frozen"],
                "user": "appuser",
            },
        }
        deployer = APIDeployer(config)
        mock_runner = MagicMock()
        deployer.runner = mock_runner

        with (
            patch.dict(
                "os.environ",
                {"FRAISIER_INSTALL_SOCKET_MY_API_PRE_PRODUCTION": str(socket_file)},
            ),
            patch.object(deployer, "_install_via_socket") as mock_socket,
        ):
            deployer._install_dependencies()

        mock_socket.assert_called_once()
        mock_runner.run.assert_not_called()


class TestInstallViaSocketMethod:
    """Unit tests for _install_via_socket."""

    def test_sends_correct_json_and_returns_on_success(self, tmp_path):
        """Connects to socket, sends JSON request, succeeds on ok=true response."""
        import json
        import socket as _socket
        import threading

        sock_path = str(tmp_path / "test.sock")
        server_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        server_sock.bind(sock_path)
        server_sock.listen(1)

        response_payload = (
            json.dumps(
                {"ok": True, "stdout": "", "stderr": "", "returncode": 0}
            ).encode()
            + b"\n"
        )

        def _serve():
            conn, _ = server_sock.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(response_payload)
            server_sock.close()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()

        cfg = {"app_path": "/var/www/api", "fraise_name": "api", "environment": "prod"}
        deployer = APIDeployer(cfg)
        # Should not raise
        deployer._install_via_socket(
            sock_path, ["uv", "sync", "--frozen"], "/var/www/api"
        )
        t.join(timeout=2)

    def test_raises_deployment_error_on_ok_false(self, tmp_path):
        """Raises DeploymentError when response ok=false."""
        import json
        import socket as _socket
        import threading

        from fraisier.errors import DeploymentError

        sock_path = str(tmp_path / "test.sock")
        server_sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        server_sock.bind(sock_path)
        server_sock.listen(1)

        response_payload = (
            json.dumps(
                {
                    "ok": False,
                    "stdout": "",
                    "stderr": "lock file out of date",
                    "returncode": 1,
                }
            ).encode()
            + b"\n"
        )

        def _serve():
            conn, _ = server_sock.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(response_payload)
            server_sock.close()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()

        cfg = {"app_path": "/var/www/api", "fraise_name": "api", "environment": "prod"}
        deployer = APIDeployer(cfg)
        with pytest.raises(DeploymentError, match="Install command failed"):
            deployer._install_via_socket(
                sock_path, ["uv", "sync", "--frozen"], "/var/www/api"
            )
        t.join(timeout=2)

    def test_raises_deployment_error_on_connection_failure(self, tmp_path):
        """Raises DeploymentError when socket connection fails."""
        from fraisier.errors import DeploymentError

        cfg = {"app_path": "/var/www/api", "fraise_name": "api", "environment": "prod"}
        deployer = APIDeployer(cfg)
        with pytest.raises(DeploymentError, match="Failed to connect"):
            deployer._install_via_socket(
                str(tmp_path / "nonexistent.sock"),
                ["uv", "sync"],
                "/var/www/api",
            )


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

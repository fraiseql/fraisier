"""Tests for deployment implementations."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentResult, DeploymentStatus
from fraisier.deployers.etl import ETLDeployer
from fraisier.deployers.scheduled import ScheduledDeployer
from fraisier.errors import DeploymentError
from fraisier.strategies import StrategyResult


class TestAPIDeployer:
    """Tests for API deployer."""

    def test_init(self):
        """Test APIDeployer initialization."""
        config = {
            "app_path": "/var/www/api",
            "systemd_service": "api.service",
            "git_repo": "https://github.com/test/api.git",
            "health_check": {"url": "http://localhost:8000/health", "timeout": 10},
        }
        deployer = APIDeployer(config)

        assert deployer.app_path == "/var/www/api"
        assert deployer.systemd_service == "api.service"
        assert deployer.git_repo == "https://github.com/test/api.git"
        assert deployer.health_check_url == "http://localhost:8000/health"
        assert deployer.health_check_timeout == 10

    def test_get_current_version_success(self, mock_subprocess):
        """Test getting current deployed version."""
        mock_subprocess.return_value = MagicMock(
            stdout="abc123def456abcd\n", returncode=0
        )

        deployer = APIDeployer({"app_path": "/var/www/api"})
        version = deployer.get_current_version()

        assert version == "abc123de"
        mock_subprocess.assert_called_once()

    def test_get_current_version_failure(self, mock_subprocess):
        """Test getting current version when git fails."""
        from subprocess import CalledProcessError

        mock_subprocess.side_effect = CalledProcessError(1, "git")

        deployer = APIDeployer({"app_path": "/var/www/api"})
        version = deployer.get_current_version()

        assert version is None

    def test_get_latest_version_success(self, mock_subprocess, tmp_path):
        """Test getting latest version from bare repo."""
        mock_subprocess.return_value = MagicMock(
            stdout="fedcba9876543210\n", returncode=0
        )
        bare_repo = tmp_path / "test.git"
        bare_repo.mkdir()

        deployer = APIDeployer(
            {
                "fraise_name": "test",
                "repos_base": str(tmp_path),
            }
        )
        version = deployer.get_latest_version()

        assert version == "fedcba98"
        mock_subprocess.assert_called_once()

    def test_execute_success(self, mock_subprocess, mock_requests, tmp_path):
        """Test successful API deployment."""
        app_dir = tmp_path / "api"
        app_dir.mkdir()
        config = {
            "app_path": str(app_dir),
            "systemd_service": "api.service",
            "health_check": {"url": "http://localhost:8000/health"},
            "database": {"strategy": "apply"},
        }

        deployer = APIDeployer(config)

        # Mock git pull success
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with patch("fraisier.strategies.get_strategy") as mock_factory:
            mock_strategy = MagicMock()
            mock_strategy.execute.return_value = StrategyResult(success=True)
            mock_factory.return_value = mock_strategy

            result = deployer.execute()

        assert result.success is True
        assert result.status == DeploymentStatus.SUCCESS
        assert result.duration_seconds > 0

    def test_execute_handles_git_pull_failure(self, mock_subprocess):
        """Test deployment fails when git pull fails."""
        from subprocess import CalledProcessError

        config = {
            "app_path": "/var/www/api",
            "systemd_service": "api.service",
        }

        deployer = APIDeployer(config)

        # Mock git pull failure
        mock_subprocess.side_effect = CalledProcessError(1, "git pull")

        result = deployer.execute()

        assert result.success is False
        assert result.status == DeploymentStatus.FAILED
        assert "Deployment failed" in result.error_message or result.error_message

    def test_fetch_and_checkout_called_during_execute(
        self,
        mock_subprocess,
        mock_requests,
    ):
        """Test execute uses bare repo fetch_and_checkout."""
        config = {
            "app_path": "/var/www/api",
            "clone_url": "git@github.com:org/api.git",
            "fraise_name": "api",
            "repos_base": "/tmp/repos",
        }
        deployer = APIDeployer(config)

        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="abc123\n",
        )

        with (
            patch(
                "fraisier.deployers.mixins.clone_bare_repo",
            ) as mock_clone,
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ) as mock_fc,
        ):
            deployer.execute()

        mock_clone.assert_called_once()
        mock_fc.assert_called_once()

    def test_restart_service_calls_systemctl(self, mock_subprocess):
        """Test service restart uses correct systemctl command."""
        deployer = APIDeployer({"systemd_service": "api.service"})

        deployer._restart_service()

        mock_subprocess.assert_called_once()
        args, _kwargs = mock_subprocess.call_args
        assert args[0] == ["sudo", "systemctl", "restart", "api.service"]

    def test_wait_for_health_success(self):
        """Test health check succeeds via HealthCheckManager."""
        from fraisier.health_check import HealthCheckResult

        deployer = APIDeployer(
            {"health_check": {"url": "http://localhost:8000/health"}}
        )
        ok = HealthCheckResult(success=True, check_type="http", duration=0.1)

        with patch("fraisier.deployers.api.HealthCheckManager") as MockMgr:
            MockMgr.return_value.check_with_retries.return_value = ok
            result = deployer._wait_for_health()

        assert result is True

    def test_wait_for_health_timeout(self):
        """Test health check failure via HealthCheckManager."""
        from fraisier.health_check import HealthCheckResult

        deployer = APIDeployer(
            {"health_check": {"url": "http://localhost:8000/health"}}
        )
        fail = HealthCheckResult(
            success=False,
            check_type="http",
            duration=5.0,
            message="Connection refused",
        )

        with patch("fraisier.deployers.api.HealthCheckManager") as MockMgr:
            MockMgr.return_value.check_with_retries.return_value = fail
            result = deployer._wait_for_health()

        assert result is False

    def test_execute_delegates_to_migrate_strategy(
        self, mock_subprocess, mock_requests, tmp_path
    ):
        """Config strategy 'apply' maps to MigrateStrategy."""
        app_dir = tmp_path / "api"
        app_dir.mkdir()
        config = {
            "app_path": str(app_dir),
            "database": {"strategy": "apply"},
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with patch("fraisier.strategies.get_strategy") as mock_factory:
            mock_strategy = MagicMock()
            mock_strategy.execute.return_value = StrategyResult(success=True)
            mock_factory.return_value = mock_strategy

            deployer.execute()

        mock_factory.assert_called_once()
        strategy_name = mock_factory.call_args[0][0]
        assert strategy_name == "migrate"
        mock_strategy.execute.assert_called_once()

    def test_execute_delegates_to_rebuild_strategy(
        self, mock_subprocess, mock_requests, tmp_path
    ):
        """Config strategy 'rebuild' maps to RebuildStrategy."""
        app_dir = tmp_path / "api"
        app_dir.mkdir()
        config = {
            "app_path": str(app_dir),
            "database": {"strategy": "rebuild"},
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with patch("fraisier.strategies.get_strategy") as mock_factory:
            mock_strategy = MagicMock()
            mock_strategy.execute.return_value = StrategyResult(success=True)
            mock_factory.return_value = mock_strategy

            deployer.execute()

        strategy_name = mock_factory.call_args[0][0]
        assert strategy_name == "rebuild"

    def test_execute_propagates_strategy_failure(
        self, mock_subprocess, mock_requests, tmp_path
    ):
        """Strategy failure propagates as deployer failure."""
        app_dir = tmp_path / "api"
        app_dir.mkdir()
        config = {
            "app_path": str(app_dir),
            "database": {"strategy": "apply"},
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with patch("fraisier.strategies.get_strategy") as mock_factory:
            mock_strategy = MagicMock()
            mock_strategy.execute.return_value = StrategyResult(
                success=False,
                errors=["Migration failed: duplicate column"],
            )
            mock_factory.return_value = mock_strategy

            result = deployer.execute()

        assert result.success is False
        assert "migration" in (result.error_message or "").lower()

    def test_execute_skips_strategy_when_no_database_config(
        self, mock_subprocess, mock_requests
    ):
        """No database config → no strategy created."""
        config = {
            "app_path": "/var/www/api",
            "systemd_service": "api.service",
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with patch("fraisier.strategies.get_strategy") as mock_factory:
            deployer.execute()

        mock_factory.assert_not_called()

    def test_execute_passes_confiture_config_to_strategy(
        self, mock_subprocess, mock_requests, tmp_path
    ):
        """Strategy receives confiture_config resolved against app_path."""
        app_dir = tmp_path / "my_api"
        app_dir.mkdir()
        config = {
            "app_path": str(app_dir),
            "database": {
                "strategy": "apply",
                "confiture_config": "custom.yaml",
            },
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with patch("fraisier.strategies.get_strategy") as mock_factory:
            mock_strategy = MagicMock()
            mock_strategy.execute.return_value = StrategyResult(success=True)
            mock_factory.return_value = mock_strategy

            deployer.execute()

        call_args = mock_strategy.execute.call_args
        assert call_args[0][0] == app_dir / "custom.yaml"

    def test_rollback_to_specific_version(self, mock_subprocess, mock_requests):
        """Test rollback to specific commit."""
        config = {
            "app_path": "/var/www/api",
            "systemd_service": "api.service",
            "health_check": {"url": "http://localhost:8000/health"},
        }

        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(
            stdout="current_version\n", returncode=0
        )

        result = deployer.rollback(to_version="abc123")

        assert result.success is True
        assert result.status == DeploymentStatus.ROLLED_BACK

        # Should call git checkout
        calls = mock_subprocess.call_args_list
        assert any("checkout" in str(call) for call in calls)

    def test_rollback_to_previous_sha(self, mock_subprocess, mock_requests):
        """Test rollback uses stored previous SHA."""
        deployer = APIDeployer(
            {
                "app_path": "/var/www/api",
                "systemd_service": "api.service",
                "fraise_name": "api",
                "repos_base": "/tmp/repos",
            }
        )
        deployer._previous_sha = "abc123def456"
        mock_subprocess.return_value = MagicMock(stdout="version\n", returncode=0)

        result = deployer.rollback()

        assert result.success is True
        assert result.status == DeploymentStatus.ROLLED_BACK
        calls = mock_subprocess.call_args_list
        assert any("abc123def456" in str(c) for c in calls)

    def test_validate_wrapper_scripts_no_env_vars(self, monkeypatch):
        """Test validation passes when no wrapper env vars are set."""
        deployer = APIDeployer({"app_path": "/var/www/api"})
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_WRAPPER", raising=False)
        monkeypatch.delenv("FRAISIER_PG_WRAPPER", raising=False)

        # Should not raise
        deployer._validate_wrapper_scripts()

    def test_validate_wrapper_scripts_all_exist(self, monkeypatch, tmp_path):
        """Test validation passes when all wrapper scripts exist and are executable."""
        systemctl_wrapper = tmp_path / "systemctl-wrapper"
        pg_wrapper = tmp_path / "pgadmin-wrapper"
        systemctl_wrapper.touch(mode=0o755)
        pg_wrapper.touch(mode=0o755)

        deployer = APIDeployer({"app_path": "/var/www/api"})
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_WRAPPER", str(systemctl_wrapper))
        monkeypatch.setenv("FRAISIER_PG_WRAPPER", str(pg_wrapper))

        # Should not raise
        deployer._validate_wrapper_scripts()

    def test_validate_wrapper_scripts_systemctl_missing(self, monkeypatch):
        """Test validation fails when systemctl wrapper is missing."""
        from fraisier.errors import DeploymentError

        deployer = APIDeployer({"app_path": "/var/www/api"})
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_WRAPPER", "/nonexistent/systemctl")
        monkeypatch.delenv("FRAISIER_PG_WRAPPER", raising=False)

        with patch.object(
            deployer, "_write_status"
        ):  # Mock to avoid DB calls in validation
            import pytest

            with pytest.raises(DeploymentError) as exc_info:
                deployer._validate_wrapper_scripts()

        assert "systemctl" in str(exc_info.value).lower()
        assert "not found" in str(exc_info.value).lower()
        assert exc_info.value.context["wrapper_1"]["remediation"].startswith("sudo cp")

    def test_validate_wrapper_scripts_pg_missing(self, monkeypatch):
        """Test validation fails when PostgreSQL wrapper is missing."""
        from fraisier.errors import DeploymentError

        deployer = APIDeployer({"app_path": "/var/www/api"})
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_WRAPPER", raising=False)
        monkeypatch.setenv("FRAISIER_PG_WRAPPER", "/nonexistent/pgadmin")

        import pytest

        with pytest.raises(DeploymentError) as exc_info:
            deployer._validate_wrapper_scripts()

        assert "pgadmin" in str(exc_info.value).lower()
        assert "not found" in str(exc_info.value).lower()

    def test_validate_wrapper_scripts_not_executable(self, monkeypatch, tmp_path):
        """Test validation fails when wrapper script is not executable."""
        from fraisier.errors import DeploymentError

        systemctl_wrapper = tmp_path / "systemctl-wrapper"
        systemctl_wrapper.touch(mode=0o644)  # Read-only, not executable

        deployer = APIDeployer({"app_path": "/var/www/api"})
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_WRAPPER", str(systemctl_wrapper))
        monkeypatch.delenv("FRAISIER_PG_WRAPPER", raising=False)

        import pytest

        with pytest.raises(DeploymentError) as exc_info:
            deployer._validate_wrapper_scripts()

        assert "not executable" in str(exc_info.value).lower()
        remediation = exc_info.value.context["wrapper_1"]["remediation"]
        assert remediation.startswith("sudo chmod")

    def test_validate_wrapper_scripts_both_missing(self, monkeypatch):
        """Test validation reports both missing wrappers in single error."""
        from fraisier.errors import DeploymentError

        deployer = APIDeployer({"app_path": "/var/www/api"})
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_WRAPPER", "/nonexistent/systemctl")
        monkeypatch.setenv("FRAISIER_PG_WRAPPER", "/nonexistent/pgadmin")

        import pytest

        with pytest.raises(DeploymentError) as exc_info:
            deployer._validate_wrapper_scripts()

        error_context = exc_info.value.context
        assert len(error_context) == 2
        assert "wrapper_1" in error_context
        assert "wrapper_2" in error_context

    def test_install_dependencies_failure_includes_stderr(self, mock_subprocess):
        """Test install failure includes stderr in context."""
        from subprocess import CalledProcessError

        config = {
            "app_path": "/var/www/api",
            "install": {"command": ["uv", "sync", "--frozen"]},
        }
        deployer = APIDeployer(config)

        # Mock install command failure with stderr output
        error = CalledProcessError(1, ["uv", "sync", "--frozen"])
        error.stdout = ""
        error.stderr = "error: version conflict in dependencies"
        mock_subprocess.side_effect = error

        with pytest.raises(DeploymentError) as exc_info:
            deployer._install_dependencies()

        assert "exit code 1" in str(exc_info.value)
        expected_stderr = "error: version conflict in dependencies"
        assert exc_info.value.context["stderr"] == expected_stderr

    def test_install_dependencies_failure_includes_cwd(self, mock_subprocess):
        """Test install failure includes cwd in context."""
        from subprocess import CalledProcessError

        config = {
            "app_path": "/var/www/api",
            "install": {"command": ["npm", "install"]},
        }
        deployer = APIDeployer(config)

        error = CalledProcessError(1, ["npm", "install"])
        error.stdout = ""
        error.stderr = ""
        mock_subprocess.side_effect = error

        with pytest.raises(DeploymentError) as exc_info:
            deployer._install_dependencies()

        assert exc_info.value.context["cwd"] == "/var/www/api"

    def test_install_dependencies_failure_suggested_command(self, mock_subprocess):
        """Test install failure includes suggested debugging command."""
        from subprocess import CalledProcessError

        config = {
            "app_path": "/var/www/api",
            "install": {"command": ["uv", "sync", "--frozen"]},
        }
        deployer = APIDeployer(config)

        error = CalledProcessError(1, ["uv", "sync", "--frozen"])
        error.stdout = ""
        error.stderr = ""
        mock_subprocess.side_effect = error

        with pytest.raises(DeploymentError) as exc_info:
            deployer._install_dependencies()

        suggested = exc_info.value.context["suggested_command"]
        assert suggested.startswith("cd /var/www/api")
        assert "uv sync --frozen" in suggested

    def test_install_dependencies_failure_includes_stdout(self, mock_subprocess):
        """Test install failure captures stdout output."""
        from subprocess import CalledProcessError

        config = {
            "app_path": "/var/www/api",
            "install": {"command": ["pip", "install", "-r", "requirements.txt"]},
        }
        deployer = APIDeployer(config)

        error = CalledProcessError(1, ["pip", "install", "-r", "requirements.txt"])
        error.stdout = "Installing collected packages: numpy\n"
        error.stderr = "ERROR: Could not find a version that satisfies"
        mock_subprocess.side_effect = error

        with pytest.raises(DeploymentError) as exc_info:
            deployer._install_dependencies()

        expected_in_stdout = "Installing collected packages: numpy"
        assert expected_in_stdout in exc_info.value.context["stdout"]

    def test_install_dependencies_skipped_when_no_command(self, mock_subprocess):
        """Test install is skipped when no install command configured."""
        config = {
            "app_path": "/var/www/api",
        }
        deployer = APIDeployer(config)

        deployer._install_dependencies()

        mock_subprocess.assert_not_called()

    def test_install_dependencies_with_sudo_user(self, mock_subprocess):
        """Test install command includes sudo prefix when user differs."""
        config = {
            "app_path": "/var/www/api",
            "deploy_user": "root",
            "install": {
                "command": ["uv", "sync"],
                "user": "appuser",
            },
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        deployer._install_dependencies()

        # Verify sudo was used
        args, kwargs = mock_subprocess.call_args
        assert args[0][:3] == ["sudo", "-u", "appuser"]
        assert kwargs["cwd"] == "/var/www/api"


class TestETLDeployer:
    """Tests for ETL deployer."""

    def test_init(self):
        """Test ETLDeployer initialization."""
        config = {
            "app_path": "/var/etl",
            "script_path": "scripts/pipeline.py",
        }
        deployer = ETLDeployer(config)

        assert deployer.app_path == "/var/etl"
        assert deployer.script_path == "scripts/pipeline.py"

    def test_get_current_version_from_git(self, mock_subprocess):
        """Test getting version from git repo."""
        mock_subprocess.return_value = MagicMock(stdout="abc123def456\n", returncode=0)

        deployer = ETLDeployer({"app_path": "/var/etl"})
        version = deployer.get_current_version()

        assert version == "abc123de"

    def test_get_latest_version_from_bare_repo(self, mock_subprocess, tmp_path):
        """Test that ETL latest version comes from bare repo."""
        mock_subprocess.return_value = MagicMock(
            stdout="fedcba9876543210\n", returncode=0
        )
        bare_repo = tmp_path / "pipeline.git"
        bare_repo.mkdir()

        deployer = ETLDeployer(
            {
                "fraise_name": "pipeline",
                "app_path": "/var/etl",
                "repos_base": str(tmp_path),
            }
        )
        version = deployer.get_latest_version()

        assert version == "fedcba98"

    def test_execute_success_with_bare_repo_and_script(self):
        """Test ETL deployment uses bare repo and runs script."""
        config = {
            "fraise_name": "pipeline",
            "app_path": "/var/etl",
            "script_path": "scripts/pipeline.py",
            "repos_base": "/tmp/repos",
        }

        deployer = ETLDeployer(config)

        with (
            patch(
                "fraisier.deployers.mixins.clone_bare_repo",
            ),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok\n")
            result = deployer.execute()

        assert result.success is True
        assert result.status == DeploymentStatus.SUCCESS

    def test_execute_fails_if_script_fails(self):
        """Test ETL deployment fails if script returns non-zero."""
        from subprocess import CalledProcessError

        config = {
            "fraise_name": "pipeline",
            "app_path": "/var/etl",
            "script_path": "scripts/missing.py",
            "repos_base": "/tmp/repos",
        }

        deployer = ETLDeployer(config)

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = CalledProcessError(1, "python scripts/missing.py")
            result = deployer.execute()

        assert result.success is False
        assert result.status == DeploymentStatus.FAILED

    def test_rollback_success(self, mock_subprocess):
        """Test ETL rollback using bare repo checkout."""
        deployer = ETLDeployer(
            {
                "fraise_name": "pipeline",
                "app_path": "/var/etl",
                "repos_base": "/tmp/repos",
            }
        )
        deployer._previous_sha = "abc123def456"
        mock_subprocess.return_value = MagicMock(stdout="version\n", returncode=0)

        with patch("fraisier.deployers.mixins.write_status"):
            result = deployer.rollback()

        assert result.success is True
        assert result.status == DeploymentStatus.ROLLED_BACK

        calls = mock_subprocess.call_args_list
        assert any("abc123def456" in str(c) for c in calls)

    def test_rollback_to_specific_version(self, mock_subprocess):
        """Test ETL rollback to specific commit via bare repo."""
        deployer = ETLDeployer(
            {
                "fraise_name": "pipeline",
                "app_path": "/var/etl",
                "repos_base": "/tmp/repos",
            }
        )
        mock_subprocess.return_value = MagicMock(stdout="version\n", returncode=0)

        with patch("fraisier.deployers.mixins.write_status"):
            result = deployer.rollback(to_version="abc123")

        assert result.success is True
        assert result.status == DeploymentStatus.ROLLED_BACK
        calls = mock_subprocess.call_args_list
        assert any("abc123" in str(c) for c in calls)


class TestScheduledDeployer:
    """Tests for Scheduled deployer."""

    def test_init(self):
        """Test ScheduledDeployer initialization."""
        config = {
            "systemd_timer": "backup.timer",
            "systemd_service": "backup.service",
        }
        deployer = ScheduledDeployer(config)

        assert deployer.systemd_timer == "backup.timer"
        assert deployer.systemd_service == "backup.service"

    def test_get_current_version_none_without_app_path(self):
        """Test version is None without app_path."""
        deployer = ScheduledDeployer({"systemd_timer": "backup.timer"})
        version = deployer.get_current_version()

        assert version is None

    def test_is_deployment_needed_when_timer_inactive(self, mock_subprocess):
        """Test deployment needed when timer is not active."""
        mock_subprocess.return_value = MagicMock(returncode=1)  # inactive

        deployer = ScheduledDeployer({"systemd_timer": "backup.timer"})

        assert deployer.is_deployment_needed() is True

    def test_is_deployment_needed_when_timer_active(self, mock_subprocess):
        """Test deployment not needed when timer is active."""
        mock_subprocess.return_value = MagicMock(returncode=0)  # active

        deployer = ScheduledDeployer({"systemd_timer": "backup.timer"})

        assert deployer.is_deployment_needed() is False

    def test_execute_enables_and_starts_timer(self):
        """Test scheduled deployment enables and starts timer."""
        config = {
            "fraise_name": "backup",
            "systemd_timer": "backup.timer",
        }

        deployer = ScheduledDeployer(config)

        with (
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="timer:active\n")
            result = deployer.execute()

        assert result.success is True
        assert result.status == DeploymentStatus.SUCCESS

        # Should call enable, start, and daemon-reload
        calls = [str(c) for c in mock_run.call_args_list]
        assert any("enable" in c for c in calls)
        assert any("start" in c for c in calls)
        assert any("daemon-reload" in c for c in calls)

    def test_daemon_reload_before_enable_and_start(self):
        """daemon-reload must run before enable and start."""
        config = {
            "fraise_name": "backup",
            "systemd_timer": "backup.timer",
        }

        deployer = ScheduledDeployer(config)

        with (
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="timer:active\n")
            deployer.execute()

        # Extract the systemctl subcommands in call order
        systemctl_cmds = []
        for call in mock_run.call_args_list:
            args = call[0][0]
            if "systemctl" in args:
                # The subcommand is after "systemctl" (may have "sudo" prefix)
                idx = args.index("systemctl")
                if idx + 1 < len(args):
                    systemctl_cmds.append(args[idx + 1])

        assert "daemon-reload" in systemctl_cmds
        assert "enable" in systemctl_cmds
        assert "start" in systemctl_cmds

        reload_idx = systemctl_cmds.index("daemon-reload")
        enable_idx = systemctl_cmds.index("enable")
        start_idx = systemctl_cmds.index("start")
        assert reload_idx < enable_idx, (
            f"daemon-reload at {reload_idx} should be before enable at {enable_idx}"
        )
        assert reload_idx < start_idx, (
            f"daemon-reload at {reload_idx} should be before start at {start_idx}"
        )

    def test_health_check_returns_true_when_active(self, mock_subprocess):
        """Test health check returns true when timer is active."""
        mock_subprocess.return_value = MagicMock(returncode=0)

        deployer = ScheduledDeployer({"systemd_timer": "backup.timer"})

        assert deployer.health_check() is True

    def test_health_check_returns_false_when_inactive(self, mock_subprocess):
        """Test health check returns false when timer is inactive."""
        mock_subprocess.return_value = MagicMock(returncode=1)

        deployer = ScheduledDeployer({"systemd_timer": "backup.timer"})

        assert deployer.health_check() is False

    def test_rollback_restarts_timer(self, mock_subprocess):
        """Test rollback restarts timer."""
        deployer = ScheduledDeployer(
            {"fraise_name": "backup", "systemd_timer": "backup.timer"}
        )
        mock_subprocess.return_value = MagicMock(stdout="timer:active\n", returncode=0)

        with patch("fraisier.deployers.mixins.write_status"):
            result = deployer.rollback()

        assert result.success is True
        assert result.status == DeploymentStatus.ROLLED_BACK

        # Should call restart
        calls = [str(c) for c in mock_subprocess.call_args_list]
        assert any("restart" in c for c in calls)


class TestAPIDeployerRebuildStopsService:
    """Rebuild strategy stops service before DB operations (#12)."""

    def test_rebuild_stops_service_before_strategy(self, mock_subprocess):
        """Service is stopped before rebuild strategy runs."""
        config = {
            "app_path": "/var/www/api",
            "systemd_service": "my-api.service",
            "database": {"strategy": "rebuild"},
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        call_order = []

        with (
            patch.object(
                deployer,
                "_stop_service",
                side_effect=lambda: call_order.append("stop"),
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

        assert "stop" in call_order
        assert "strategy" in call_order
        assert call_order.index("stop") < call_order.index("strategy")

    def test_migrate_does_not_stop_service(self, mock_subprocess):
        """Migrate strategy does NOT stop service before running."""
        config = {
            "app_path": "/var/www/api",
            "systemd_service": "my-api.service",
            "database": {"strategy": "apply"},
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with (
            patch.object(deployer, "_stop_service") as mock_stop,
            patch.object(deployer, "_run_strategy"),
            patch.object(deployer, "_restart_service"),
        ):
            deployer.execute()

        mock_stop.assert_not_called()


class TestAPIDeployerChdirForStrategy:
    """Deployer must chdir to app_path before running confiture."""

    def test_strategy_runs_in_app_path_cwd(
        self, mock_subprocess, mock_requests, tmp_path
    ):
        """_run_strategy() executes with cwd set to app_path."""
        app_dir = tmp_path / "my-app"
        app_dir.mkdir()
        config = {
            "app_path": str(app_dir),
            "database": {"strategy": "apply"},
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        cwd_during_strategy = []

        with patch("fraisier.strategies.get_strategy") as mock_factory:
            mock_strategy = MagicMock()

            def capture_cwd(*args, **kwargs):
                cwd_during_strategy.append(str(Path.cwd()))
                return StrategyResult(success=True)

            mock_strategy.execute.side_effect = capture_cwd
            mock_factory.return_value = mock_strategy

            deployer.execute()

        assert cwd_during_strategy
        assert cwd_during_strategy[0] == str(app_dir)

    def test_cwd_restored_after_strategy(
        self, mock_subprocess, mock_requests, tmp_path
    ):
        """Original cwd is restored after strategy runs (even on failure)."""

        app_dir = tmp_path / "my-app"
        app_dir.mkdir()
        original_cwd = str(Path.cwd())
        config = {
            "app_path": str(app_dir),
            "database": {"strategy": "apply"},
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with patch("fraisier.strategies.get_strategy") as mock_factory:
            mock_strategy = MagicMock()
            mock_strategy.execute.return_value = StrategyResult(success=True)
            mock_factory.return_value = mock_strategy

            deployer.execute()

        assert str(Path.cwd()) == original_cwd

    def test_relative_paths_resolved_against_app_path(
        self, mock_subprocess, mock_requests, tmp_path
    ):
        """Relative confiture_config and migrations_dir resolve against app_path."""
        app_dir = tmp_path / "my-app"
        app_dir.mkdir()
        config = {
            "app_path": str(app_dir),
            "database": {
                "strategy": "apply",
                "confiture_config": "db/environments/dev.yaml",
                "migrations_dir": "db/migrations",
            },
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with patch("fraisier.strategies.get_strategy") as mock_factory:
            mock_strategy = MagicMock()
            mock_strategy.execute.return_value = StrategyResult(success=True)
            mock_factory.return_value = mock_strategy

            deployer.execute()

        call_args = mock_strategy.execute.call_args
        actual_config = call_args[0][0]
        actual_migrations = call_args[1]["migrations_dir"]
        assert actual_config == app_dir / "db" / "environments" / "dev.yaml"
        assert actual_migrations == app_dir / "db" / "migrations"

    def test_absolute_paths_not_changed(self, mock_subprocess, mock_requests, tmp_path):
        """Absolute confiture_config and migrations_dir are left unchanged."""
        app_dir = tmp_path / "my-app"
        app_dir.mkdir()
        config = {
            "app_path": str(app_dir),
            "database": {
                "strategy": "apply",
                "confiture_config": "/etc/confiture/prod.yaml",
                "migrations_dir": "/opt/migrations",
            },
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with patch("fraisier.strategies.get_strategy") as mock_factory:
            mock_strategy = MagicMock()
            mock_strategy.execute.return_value = StrategyResult(success=True)
            mock_factory.return_value = mock_strategy

            deployer.execute()

        call_args = mock_strategy.execute.call_args
        actual_config = call_args[0][0]
        actual_migrations = call_args[1]["migrations_dir"]
        assert actual_config == Path("/etc/confiture/prod.yaml")
        assert actual_migrations == Path("/opt/migrations")

    def test_missing_app_path_fails_loudly(self, mock_subprocess, mock_requests):
        """Deployment fails with clear error when app_path directory is missing."""
        config = {
            "app_path": "/nonexistent/path/to/app",
            "database": {"strategy": "apply"},
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        result = deployer.execute()

        assert result.success is False
        assert "app_path does not exist" in result.error_message


class TestAPIDeployerNotifications:
    """Tests for notification wiring in APIDeployer.execute()."""

    def test_notify_called_on_success(self, mock_subprocess, mock_requests):
        """Successful deploy calls _notify with success result."""
        config = {
            "app_path": "/var/www/api",
            "health_check": {"url": "http://localhost:8000/health"},
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        with patch.object(deployer, "_notify") as mock_notify:
            result = deployer.execute()

        assert result.success is True
        mock_notify.assert_called_once_with(result)

    def test_notify_called_on_failure(self, mock_subprocess):
        """Deploy failure (exception, no rollback) calls _notify with failure result."""
        from subprocess import CalledProcessError

        config = {"app_path": "/var/www/api"}
        deployer = APIDeployer(config)
        mock_subprocess.side_effect = CalledProcessError(1, "git pull")

        with patch.object(deployer, "_notify") as mock_notify:
            result = deployer.execute()

        assert result.success is False
        mock_notify.assert_called_once_with(result)


class TestDeploymentResult:
    """Tests for DeploymentResult dataclass."""

    def test_deployment_result_success(self):
        """Test successful deployment result."""
        result = DeploymentResult(
            success=True,
            status=DeploymentStatus.SUCCESS,
            old_version="v1",
            new_version="v2",
            duration_seconds=10.5,
        )

        assert result.success is True
        assert result.status == DeploymentStatus.SUCCESS
        assert result.old_version == "v1"
        assert result.new_version == "v2"
        assert result.duration_seconds == 10.5
        assert result.error_message is None

    def test_deployment_result_failure(self):
        """Test failed deployment result."""
        result = DeploymentResult(
            success=False,
            status=DeploymentStatus.FAILED,
            error_message="Git pull failed",
        )

        assert result.success is False
        assert result.status == DeploymentStatus.FAILED
        assert result.error_message == "Git pull failed"

    def test_deployment_result_with_details(self):
        """Test deployment result with extra details."""
        details = {"reason": "script timeout", "output": "..."}
        result = DeploymentResult(
            success=False,
            status=DeploymentStatus.FAILED,
            error_message="Deployment timed out",
            details=details,
        )

        assert result.details == details


class TestWriteIncident:
    """Tests for _write_incident mixin method."""

    def test_writes_incident_file(self, tmp_path):
        deployer = APIDeployer({"app_path": "/var/www/api", "fraise_name": "my_api"})

        incidents_dir = tmp_path / "incidents"
        with patch(
            "fraisier.deployers.mixins.Path",
            return_value=incidents_dir,
        ):
            deployer._write_incident(
                "rollback failed",
                current_version="abc123",
                target_version="def456",
                db_errors=["constraint violation"],
            )

        # Should have created a JSON file
        files = list(incidents_dir.glob("*.json"))
        assert len(files) == 1

        import json

        data = json.loads(files[0].read_text())
        assert data["fraise"] == "my_api"
        assert data["error"] == "rollback failed"
        assert "constraint violation" in data["db_errors"]


class TestExecuteWithLifecycle:
    """Tests for _execute_with_lifecycle mixin method."""

    def _make_deployer(self):
        """Create a minimal ETLDeployer for lifecycle testing."""
        config = {"app_path": "/tmp/etl", "script_path": "run.py"}
        return ETLDeployer(config)

    def test_records_timing_and_success(self):
        """Lifecycle records timing and writes success status."""
        deployer = self._make_deployer()
        with patch.object(deployer, "_write_status") as ws:
            result = deployer._execute_with_lifecycle(
                lambda: ("v1", "v2"),
            )

        assert result.success is True
        assert result.status == DeploymentStatus.SUCCESS
        assert result.old_version == "v1"
        assert result.new_version == "v2"
        assert result.duration_seconds > 0
        ws.assert_any_call("deploying")
        ws.assert_any_call("success", commit_sha="v2")

    def test_handles_exception_and_records_failure(self):
        """Lifecycle catches exceptions and writes failure."""
        deployer = self._make_deployer()

        def boom():
            raise RuntimeError("kaboom")

        with patch.object(deployer, "_write_status") as ws:
            result = deployer._execute_with_lifecycle(boom)

        assert result.success is False
        assert result.status == DeploymentStatus.FAILED
        assert "kaboom" in result.error_message
        ws.assert_any_call("deploying")
        ws.assert_any_call("failed", error_message="kaboom")

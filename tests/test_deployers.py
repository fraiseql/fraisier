"""Tests for deployment implementations."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentResult, DeploymentStatus
from fraisier.deployers.etl import ETLDeployer
from fraisier.deployers.scheduled import ScheduledDeployer
from fraisier.errors import DeploymentError
from fraisier.service_managers import get_service_manager
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
        assert result.error_message is not None

    def test_post_pull_config_sync_failure_aborts_before_install(self, tmp_path):
        """A failed post-pull scaffold re-bake is FATAL and aborts before the
        install step (#279).

        Previously non-fatal: the deploy logged an error and continued into a
        masked ``command not allowed`` at the install step. Now the post-pull
        sync (which re-bakes the install-helper allowlist) hard-gates the
        deploy — it fails there, naming the likely stale allowlist.
        """
        config = {
            "app_path": str(tmp_path),
            "systemd_service": "api.service",
            "health_check": {"url": "http://localhost:8000/health"},
        }

        deployer = APIDeployer(config)

        # Track calls to _sync_config_if_needed; fail only the 2nd (post-pull).
        call_count = {"count": 0}

        def sync_config_side_effect():
            call_count["count"] += 1
            if call_count["count"] == 2:
                raise DeploymentError("boom: command not allowed")

        with (
            patch.object(deployer, "_validate_wrapper_scripts"),
            patch.object(deployer, "_check_service_file_staleness"),
            patch.object(deployer, "_git_pull", return_value=("abc", "def")),
            patch.object(
                deployer,
                "_sync_config_if_needed",
                side_effect=sync_config_side_effect,
            ),
            patch.object(deployer, "_install_dependencies") as mock_install,
            patch.object(deployer, "_write_status"),
            patch.object(deployer, "_start_db_record", return_value=None),
            patch.object(deployer, "_complete_db_record"),
            patch.object(deployer, "_notify"),
            patch.object(deployer, "_wrap_error"),
            patch.object(deployer, "_restore_previous_state"),
            patch.object(deployer, "_restore_version_json"),
        ):
            result = deployer.execute()

        # Fatal: deploy failed, and aborted BEFORE the install step, so the
        # stale-allowlist "command not allowed" is never reached.
        assert result.success is False
        mock_install.assert_not_called()
        # The surfaced error names the likely cause + the issue.
        assert result.error_message is not None
        assert "279" in result.error_message
        lowered = result.error_message.lower()
        assert "install.command" in lowered or "allowlist" in lowered

    def test_pre_pull_config_sync_failure_is_nonfatal(self, tmp_path):
        """A pre-pull sync failure stays non-fatal (#279).

        The pre-pull sync runs from the old, pre-checkout worktree, whose
        install.command still matches the baked allowlist — not the trap. Only
        the post-pull re-sync hard-gates. Reaching git pull proves the pre-pull
        failure did not abort the deploy.
        """
        config = {
            "app_path": str(tmp_path),
            "systemd_service": "api.service",
            "health_check": {"url": "http://localhost:8000/health"},
        }
        deployer = APIDeployer(config)

        call_count = {"count": 0}

        def sync_config_side_effect():
            call_count["count"] += 1
            if call_count["count"] == 1:
                raise DeploymentError("pre-pull boom")

        with (
            patch.object(deployer, "_validate_wrapper_scripts"),
            patch.object(deployer, "_check_service_file_staleness"),
            # Stop the deploy right after git pull so the full pipeline isn't
            # exercised; reaching git pull is what proves pre-pull was non-fatal.
            patch.object(
                deployer, "_git_pull", side_effect=RuntimeError("stop")
            ) as mock_pull,
            patch.object(
                deployer, "_sync_config_if_needed", side_effect=sync_config_side_effect
            ),
            patch.object(deployer, "_write_status"),
            patch.object(deployer, "_start_db_record", return_value=None),
            patch.object(deployer, "_complete_db_record"),
            patch.object(deployer, "_notify"),
            patch.object(deployer, "_wrap_error"),
            patch.object(deployer, "_restore_previous_state"),
            patch.object(deployer, "_restore_version_json"),
        ):
            result = deployer.execute()

        assert result.success is False
        mock_pull.assert_called_once()

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

    def test_restart_service_uses_service_manager(self):
        """Test service restart uses ServiceManager abstraction."""
        deployer = APIDeployer({"systemd_service": "api.service"})
        mock_service_manager = MagicMock()

        with patch("fraisier.service_managers.get_service_manager") as mock_get:
            mock_get.return_value = mock_service_manager

            deployer._restart_service()

            mock_get.assert_called_once()
            mock_service_manager.restart.assert_called_once_with("api.service")

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

        # Should not raise
        deployer._validate_wrapper_scripts()

    def test_validate_wrapper_scripts_systemctl_exists(self, monkeypatch, tmp_path):
        """Test validation passes when systemctl wrapper exists and is executable."""
        systemctl_wrapper = tmp_path / "systemctl-wrapper"
        systemctl_wrapper.touch(mode=0o755)

        deployer = APIDeployer({"app_path": "/var/www/api"})
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_WRAPPER", str(systemctl_wrapper))

        # Should not raise
        deployer._validate_wrapper_scripts()

    def test_validate_wrapper_scripts_systemctl_missing(self, monkeypatch):
        """Test validation fails when systemctl wrapper is missing."""
        from fraisier.errors import DeploymentError

        deployer = APIDeployer({"app_path": "/var/www/api"})
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_WRAPPER", "/nonexistent/systemctl")

        with patch.object(
            deployer, "_write_status"
        ):  # Mock to avoid DB calls in validation
            import pytest

            with pytest.raises(DeploymentError) as exc_info:
                deployer._validate_wrapper_scripts()

        assert "systemctl" in str(exc_info.value).lower()
        assert "not found" in str(exc_info.value).lower()
        assert exc_info.value.context["wrapper_1"]["remediation"].startswith("sudo cp")

    def test_validate_wrapper_scripts_not_executable(self, monkeypatch, tmp_path):
        """Test validation fails when wrapper script is not executable."""
        from fraisier.errors import DeploymentError

        systemctl_wrapper = tmp_path / "systemctl-wrapper"
        systemctl_wrapper.touch(mode=0o644)  # Read-only, not executable

        deployer = APIDeployer({"app_path": "/var/www/api"})
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_WRAPPER", str(systemctl_wrapper))

        import pytest

        with pytest.raises(DeploymentError) as exc_info:
            deployer._validate_wrapper_scripts()

        assert "not executable" in str(exc_info.value).lower()
        remediation = exc_info.value.context["wrapper_1"]["remediation"]
        assert remediation.startswith("sudo chmod")

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
        assert args[0][:4] == ["sudo", "-H", "-u", "appuser"]
        assert kwargs["cwd"] == "/var/www/api"

    def test_sync_config_uses_fraisier_config_env_var(self, tmp_path, monkeypatch):
        """_sync_config_if_needed uses FRAISIER_CONFIG env var as destination."""
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "fraises.yaml").write_text("name: test")
        expected_dest = tmp_path / "custom" / "fraises.yaml"

        monkeypatch.setenv("FRAISIER_CONFIG", str(expected_dest))

        deployer = APIDeployer({"fraise_name": "api", "app_path": str(app_dir)})
        with (
            patch.object(deployer, "_sync_fraises_yaml") as mock_sync,
            patch.object(deployer, "_detect_config_changes", return_value=False),
        ):
            deployer._sync_config_if_needed()

        mock_sync.assert_called_once()
        assert mock_sync.call_args.kwargs["dest_path"] == expected_dest

    def test_sync_config_falls_back_to_default_path(self, tmp_path, monkeypatch):
        """_sync_config_if_needed falls back to default path when env var absent."""
        monkeypatch.delenv("FRAISIER_CONFIG", raising=False)

        app_dir = tmp_path / "app"
        app_dir.mkdir()
        (app_dir / "fraises.yaml").write_text("name: test")

        deployer = APIDeployer({"fraise_name": "api", "app_path": str(app_dir)})
        with (
            patch.object(deployer, "_sync_fraises_yaml") as mock_sync,
            patch.object(deployer, "_detect_config_changes", return_value=False),
        ):
            deployer._sync_config_if_needed()

        mock_sync.assert_called_once()
        default = Path("/opt/fraisier/fraises.yaml")
        assert mock_sync.call_args.kwargs["dest_path"] == default

    def _write_state_dir_config(self, tmp_path):
        """Write an opt-config pinning an explicit scaffold.state_dir.

        Returns (opt_config_path, state_dir_str).
        """
        state_dir = tmp_path / "srv" / "myproject" / "scaffold"
        opt_config = tmp_path / "opt" / "fraisier" / "fraises.yaml"
        opt_config.parent.mkdir(parents=True)
        opt_config.write_text(
            f"name: myproject\nscaffold:\n  state_dir: {state_dir}\nfraises: {{}}\n"
        )
        return opt_config, str(state_dir)

    def test_socket_present_but_unreachable_logs_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        """A present-but-dead scaffold-install socket warns, not silently degrades (#283).

        When the helper daemon fails to start (e.g. its baked install.sh is
        missing), the socket path exists but connections fail — the deploy must
        surface that loudly rather than quietly fall through to the neutered
        subprocess path.
        """
        import logging

        from fraisier.deployers import base as base_mod

        opt_config = tmp_path / "fraises.yaml"
        opt_config.write_text("name: myproject\nfraises: {}\n")
        fake_socket = tmp_path / "scaffold-install.sock"
        fake_socket.write_text("")  # a plain file, not a listening socket
        monkeypatch.setattr(
            base_mod, "_get_scaffold_socket_path", lambda _p: str(fake_socket)
        )

        deployer = APIDeployer({"fraise_name": "api", "app_path": str(tmp_path)})
        with caplog.at_level(logging.WARNING, logger="fraisier"):
            result = deployer._try_scaffold_install_via_socket(opt_config)

        assert result is None
        assert any("present but" in r.message for r in caplog.records)

    def test_socket_absent_falls_back_quietly(self, tmp_path, monkeypatch, caplog):
        """An absent socket (pre-helper deploy) falls back without a warning (#283)."""
        import logging

        from fraisier.deployers import base as base_mod

        opt_config = tmp_path / "fraises.yaml"
        opt_config.write_text("name: myproject\nfraises: {}\n")
        missing = tmp_path / "nope.sock"
        monkeypatch.setattr(
            base_mod, "_get_scaffold_socket_path", lambda _p: str(missing)
        )

        deployer = APIDeployer({"fraise_name": "api", "app_path": str(tmp_path)})
        with caplog.at_level(logging.WARNING, logger="fraisier"):
            result = deployer._try_scaffold_install_via_socket(opt_config)

        assert result is None
        assert not any("present but" in r.message for r in caplog.records)

    def test_regenerate_scaffold_renders_into_state_dir(self, tmp_path):
        """Regeneration renders into the server-side scaffold state tree (#283).

        The scaffold-install-helper socket runs a baked ``install.sh`` in the
        single project-level ``state_dir``.  Regeneration must materialize that
        same tree (via ``scaffold --output-dir <state_dir>``) so a changed
        ``install.command`` reaches the installed unit — independently of the
        per-env ``app_path`` or the CWD-relative ``output_dir``.
        """
        opt_config, state_dir = self._write_state_dir_config(tmp_path)

        runner = MagicMock()
        runner.run.return_value = MagicMock(returncode=0, stdout="")
        deployer = APIDeployer(
            {"fraise_name": "api", "app_path": str(tmp_path / "var/www/api")},
            runner=runner,
        )

        with patch.object(
            deployer, "_get_fraisier_executable", return_value="fraisier"
        ):
            deployer._regenerate_scaffold(config_path=opt_config)

        run_call = runner.run.call_args
        assert run_call.args[0] == [
            "fraisier",
            "-c",
            str(opt_config),
            "scaffold",
            "--output-dir",
            state_dir,
        ]

    def test_install_scaffold_fallback_reads_from_state_dir(self, tmp_path):
        """The subprocess-fallback install reads from the state tree (#283).

        When the scaffold-install-helper socket is unavailable, the fallback
        ``scaffold-install`` must read the generated units from the same
        ``state_dir`` regeneration wrote to.
        """
        opt_config, state_dir = self._write_state_dir_config(tmp_path)

        runner = MagicMock()
        runner.run.return_value = MagicMock(returncode=0, stdout="")
        deployer = APIDeployer(
            {"fraise_name": "api", "app_path": str(tmp_path / "var/www/api")},
            runner=runner,
        )

        with (
            patch.object(deployer, "_get_fraisier_executable", return_value="fraisier"),
            patch.object(
                deployer, "_try_scaffold_install_via_socket", return_value=None
            ),
        ):
            deployer._install_scaffold(config_path=opt_config)

        run_call = runner.run.call_args
        assert run_call.args[0] == [
            "fraisier",
            "-c",
            str(opt_config),
            "scaffold-install",
            "--output-dir",
            state_dir,
            "--yes",
        ]

    def test_sync_config_called_after_git_pull(self, mock_subprocess):
        """_sync_config_if_needed is called after _git_pull to pick up new fraises.yaml.

        Regression test for issue #158: bootstrap ordering problem where the
        deploy ran with stale cached config when fraises.yaml changed in the
        incoming commit.
        """
        call_order = []

        config = {
            "app_path": "/var/www/api",
            "fraise_name": "api",
        }
        deployer = APIDeployer(config)

        def record_sync(*_args, **_kwargs):
            call_order.append("sync")

        def record_pull(*_args, **_kwargs):
            call_order.append("pull")
            return ("abc", "def")

        with (
            patch.object(deployer, "_sync_config_if_needed", side_effect=record_sync),
            patch.object(deployer, "_git_pull", side_effect=record_pull),
            patch.object(deployer, "_check_service_file_staleness"),
            patch.object(deployer, "_validate_wrapper_scripts"),
            patch.object(deployer, "_install_dependencies"),
            patch.object(deployer, "_generate_version_json"),
            patch.object(deployer, "_write_status"),
            patch.object(deployer, "_start_db_record", return_value=None),
            patch.object(deployer, "_complete_db_record"),
            patch.object(deployer, "_notify"),
        ):
            deployer.execute()

        pull_index = call_order.index("pull")
        assert any(v == "sync" for v in call_order[pull_index + 1 :]), (
            "_sync_config_if_needed was never called after _git_pull"
        )

    def test_install_dependencies_with_different_users(self, tmp_path, mock_subprocess):
        """_install_dependencies uses sudo -u when install_user differs."""
        venv = tmp_path / ".venv"
        venv.mkdir()

        config = {
            "app_path": str(tmp_path),
            "deploy_user": "deployuser",
            "install": {
                "command": ["uv", "sync", "--frozen"],
                "user": "appuser",
            },
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        deployer._install_dependencies()

        # Should call install command with sudo -u appuser
        assert mock_subprocess.call_count == 1
        install_call = mock_subprocess.call_args_list[0][0][0]
        assert install_call[:4] == ["sudo", "-H", "-u", "appuser"]

    def test_install_dependencies_skips_chown_when_no_venv(
        self, tmp_path, mock_subprocess
    ):
        """`_install_dependencies` skips chown when .venv doesn't exist."""
        config = {
            "app_path": str(tmp_path),
            "deploy_user": "deployuser",
            "install": {
                "command": ["uv", "sync", "--frozen"],
                "user": "appuser",
            },
        }
        deployer = APIDeployer(config)
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="")

        deployer._install_dependencies()

        assert mock_subprocess.call_count == 1
        install_call = mock_subprocess.call_args_list[0][0][0]
        assert install_call[:4] == ["sudo", "-H", "-u", "appuser"]

    def test_sync_config_saves_hash_after_successful_install(
        self, tmp_path, monkeypatch
    ):
        """_sync_config_if_needed calls save_hash after successful scaffold install."""
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        opt_dir = tmp_path / "opt"
        opt_dir.mkdir()
        opt_config = opt_dir / "fraises.yaml"
        opt_config.write_text("new: config")

        (app_dir / "fraises.yaml").write_text("new: config")
        monkeypatch.setenv("FRAISIER_CONFIG", str(opt_config))

        deployer = APIDeployer({"fraise_name": "api", "app_path": str(app_dir)})

        with (
            patch.object(deployer, "_detect_config_changes", return_value=True),
            patch.object(deployer, "_regenerate_scaffold"),
            patch.object(deployer, "_install_scaffold"),
            patch("fraisier.config_watcher.ConfigWatcher") as mock_watcher_class,
        ):
            mock_watcher = MagicMock()
            mock_watcher_class.return_value = mock_watcher
            deployer._sync_config_if_needed()

        # Verify ConfigWatcher was instantiated with the right directory
        mock_watcher_class.assert_called_once_with(opt_dir)
        # Verify save_hash was called
        mock_watcher.save_hash.assert_called_once()

    def test_sync_config_saves_hash_even_if_install_fails(self, tmp_path, monkeypatch):
        """_sync_config_if_needed saves hash after regenerate, even if install fails.

        The hash records "what config produced the scaffold output directory",
        not "what is currently installed".  Persisting before install prevents
        an indefinite regenerate+install loop when install is broken (#193).
        """
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        opt_dir = tmp_path / "opt"
        opt_dir.mkdir()
        opt_config = opt_dir / "fraises.yaml"
        opt_config.write_text("new: config")

        (app_dir / "fraises.yaml").write_text("new: config")
        monkeypatch.setenv("FRAISIER_CONFIG", str(opt_config))

        deployer = APIDeployer({"fraise_name": "api", "app_path": str(app_dir)})

        with (
            patch.object(deployer, "_detect_config_changes", return_value=True),
            patch.object(deployer, "_regenerate_scaffold"),
            patch.object(
                deployer,
                "_install_scaffold",
                side_effect=DeploymentError("Install failed"),
            ),
            patch("fraisier.config_watcher.ConfigWatcher") as mock_watcher_class,
        ):
            mock_watcher = MagicMock()
            mock_watcher_class.return_value = mock_watcher

            with pytest.raises(DeploymentError):
                deployer._sync_config_if_needed()

        # Verify save_hash WAS called (hash persisted before install attempt)
        mock_watcher.save_hash.assert_called_once()

    def test_sync_config_skips_scaffold_when_unchanged(self, tmp_path, monkeypatch):
        """_sync_config_if_needed skips scaffold regen when config unchanged."""
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        opt_dir = tmp_path / "opt"
        opt_dir.mkdir()
        opt_config = opt_dir / "fraises.yaml"
        opt_config.write_text("same: config")

        (app_dir / "fraises.yaml").write_text("same: config")
        monkeypatch.setenv("FRAISIER_CONFIG", str(opt_config))

        deployer = APIDeployer({"fraise_name": "api", "app_path": str(app_dir)})

        with (
            patch.object(deployer, "_detect_config_changes", return_value=False),
            patch.object(deployer, "_regenerate_scaffold") as mock_regen,
            patch.object(deployer, "_install_scaffold") as mock_install,
            patch("fraisier.config_watcher.ConfigWatcher") as mock_watcher_class,
        ):
            mock_watcher = MagicMock()
            mock_watcher_class.return_value = mock_watcher
            deployer._sync_config_if_needed()

        # Neither regenerate nor install should be called
        mock_regen.assert_not_called()
        mock_install.assert_not_called()
        # save_hash should not be called either
        mock_watcher.save_hash.assert_not_called()


class TestVersionJsonAbortRollback:
    """version.json must not be left advanced when a deploy aborts (issue #257).

    version.json is regenerated *before* migrations run (the rebuild strategy
    reads it to stamp app_version into the DB), so an aborted deploy would
    otherwise leave it advanced ahead of the actual schema — making /health and
    `fraisier health` report a version that was never successfully deployed.
    """

    def _migrate_deployer(self, app_dir):
        return APIDeployer(
            {"app_path": str(app_dir), "database": {"strategy": "apply"}}
        )

    def test_failed_migration_restores_prior_version_json(self, tmp_path):
        """A migrate failure rolls version.json back to the pre-deploy value."""
        app_dir = tmp_path / "api"
        app_dir.mkdir()
        version_file = app_dir / "version.json"
        version_file.write_text('{"version": "1.0.0"}\n')

        deployer = self._migrate_deployer(app_dir)

        def advance_version():
            version_file.write_text('{"version": "2.0.0"}\n')

        with (
            patch.object(deployer, "_git_pull", return_value=("oldsha", "newsha")),
            patch.object(deployer, "_install_dependencies"),
            patch.object(deployer, "_check_service_file_staleness"),
            patch.object(deployer, "_validate_wrapper_scripts"),
            patch.object(deployer, "_sync_config_if_needed"),
            patch.object(
                deployer, "_generate_version_json", side_effect=advance_version
            ),
            patch.object(
                deployer,
                "_run_database_migrations",
                side_effect=DeploymentError("migrate boom"),
            ),
            patch.object(deployer, "_restore_previous_state"),
            patch.object(deployer, "_write_status"),
            patch.object(deployer, "_start_db_record", return_value=None),
            patch.object(deployer, "_complete_db_record"),
            patch.object(deployer, "_notify"),
        ):
            result = deployer.execute()

        assert result.success is False
        assert json.loads(version_file.read_text())["version"] == "1.0.0"

    def test_failed_migration_removes_version_json_when_none_existed(self, tmp_path):
        """A first-deploy migrate failure removes the prematurely-written file."""
        app_dir = tmp_path / "api"
        app_dir.mkdir()
        version_file = app_dir / "version.json"

        deployer = self._migrate_deployer(app_dir)

        def advance_version():
            version_file.write_text('{"version": "2.0.0"}\n')

        with (
            patch.object(deployer, "_git_pull", return_value=(None, "newsha")),
            patch.object(deployer, "_install_dependencies"),
            patch.object(deployer, "_check_service_file_staleness"),
            patch.object(deployer, "_validate_wrapper_scripts"),
            patch.object(deployer, "_sync_config_if_needed"),
            patch.object(
                deployer, "_generate_version_json", side_effect=advance_version
            ),
            patch.object(
                deployer,
                "_run_database_migrations",
                side_effect=DeploymentError("migrate boom"),
            ),
            patch.object(deployer, "_restore_previous_state"),
            patch.object(deployer, "_write_status"),
            patch.object(deployer, "_start_db_record", return_value=None),
            patch.object(deployer, "_complete_db_record"),
            patch.object(deployer, "_notify"),
        ):
            result = deployer.execute()

        assert result.success is False
        assert not version_file.exists()

    def test_successful_deploy_keeps_new_version_json(self, tmp_path):
        """A clean deploy leaves the freshly generated version.json in place."""
        app_dir = tmp_path / "api"
        app_dir.mkdir()
        version_file = app_dir / "version.json"
        version_file.write_text('{"version": "1.0.0"}\n')

        deployer = self._migrate_deployer(app_dir)

        def advance_version():
            version_file.write_text('{"version": "2.0.0"}\n')

        with (
            patch.object(deployer, "_git_pull", return_value=("oldsha", "newsha")),
            patch.object(deployer, "_install_dependencies"),
            patch.object(deployer, "_check_service_file_staleness"),
            patch.object(deployer, "_validate_wrapper_scripts"),
            patch.object(deployer, "_sync_config_if_needed"),
            patch.object(
                deployer, "_generate_version_json", side_effect=advance_version
            ),
            patch.object(deployer, "_run_database_migrations"),
            patch.object(deployer, "_run_post_migrate"),
            patch.object(deployer, "_write_status"),
            patch.object(deployer, "_start_db_record", return_value=None),
            patch.object(deployer, "_complete_db_record"),
            patch.object(deployer, "_notify"),
        ):
            result = deployer.execute()

        assert result.success is True
        assert json.loads(version_file.read_text())["version"] == "2.0.0"

    def test_health_check_rollback_restores_prior_version_json(self, tmp_path):
        """A post-deploy health-check rollback also reverts version.json (#257)."""
        app_dir = tmp_path / "api"
        app_dir.mkdir()
        version_file = app_dir / "version.json"
        version_file.write_text('{"version": "1.0.0"}\n')

        config = {
            "app_path": str(app_dir),
            "systemd_service": "api.service",
            "health_check": {"url": "http://localhost:8000/health"},
            "database": {"strategy": "apply"},
        }
        deployer = APIDeployer(config)
        deployer._previous_sha = "oldsha"

        def advance_version():
            version_file.write_text('{"version": "2.0.0"}\n')

        with (
            patch.object(deployer, "_git_pull", return_value=("oldsha", "newsha")),
            patch.object(deployer, "_install_dependencies"),
            patch.object(deployer, "_check_service_file_staleness"),
            patch.object(deployer, "_validate_wrapper_scripts"),
            patch.object(deployer, "_sync_config_if_needed"),
            patch.object(
                deployer, "_generate_version_json", side_effect=advance_version
            ),
            patch.object(deployer, "_run_database_migrations"),
            patch.object(deployer, "_run_post_migrate"),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=False),
            patch.object(deployer, "_git_rollback"),
            patch.object(deployer, "get_current_version", return_value="oldsha"),
            patch.object(deployer, "_write_status"),
            patch.object(deployer, "_start_db_record", return_value=None),
            patch.object(deployer, "_complete_db_record"),
            patch.object(deployer, "_notify"),
        ):
            result = deployer.execute()

        assert result.success is False
        assert result.status == DeploymentStatus.ROLLED_BACK
        assert json.loads(version_file.read_text())["version"] == "1.0.0"


class TestAPIDeployerRebuildAppVersion:
    """APIDeployer plumbs database.app_version through to RebuildStrategy."""

    def test_reads_app_version_from_database_config(self):
        deployer = APIDeployer(
            {
                "app_path": "/tmp/x",
                "database": {
                    "strategy": "rebuild",
                    "create_template": True,
                    "app_version": "9.9.9",
                },
            }
        )
        strategy, *_ = deployer._resolve_strategy()
        assert strategy._app_version == "9.9.9"

    def test_omits_app_version_when_unset(self):
        deployer = APIDeployer(
            {
                "app_path": "/tmp/x",
                "database": {
                    "strategy": "rebuild",
                    "create_template": True,
                },
            }
        )
        strategy, *_ = deployer._resolve_strategy()
        assert strategy._app_version is None

    def test_invalid_app_version_propagates(self):
        deployer = APIDeployer(
            {
                "app_path": "/tmp/x",
                "database": {
                    "strategy": "rebuild",
                    "create_template": True,
                    "app_version": "1!2.3.4",
                },
            }
        )
        with pytest.raises(ValueError, match="app_version"):
            deployer._resolve_strategy()


class TestServiceFileStaleness:
    """_check_service_file_staleness warns when live unit differs from scaffold."""

    def _make_deployer(self, tmp_path, svc="myapp-api.service"):
        from fraisier.deployers.api import APIDeployer

        return APIDeployer(
            {
                "app_path": str(tmp_path),
                "systemd_service": svc,
                "scaffold": {"output_dir": "scripts/generated"},
            }
        )

    def test_warns_when_live_differs_from_generated(self, tmp_path, caplog):
        """Warning emitted when generated and live service files diverge."""
        import logging

        svc = "myapp-api.service"
        generated = tmp_path / "generated.service"
        live = tmp_path / "live.service"
        generated.write_text("[Service]\nExecStart=/new/path\n")
        live.write_text("[Service]\nExecStart=/old/path\n")

        deployer = self._make_deployer(tmp_path, svc)
        with caplog.at_level(logging.WARNING, logger="fraisier"):
            deployer._check_service_file_staleness_paths(generated, live)

        assert any("out of sync" in r.message for r in caplog.records)

    def test_no_warning_when_files_match(self, tmp_path, caplog):
        """No warning when generated and live service files are identical."""
        import logging

        content = "[Service]\nExecStart=/same/path\n"
        generated = tmp_path / "generated.service"
        live = tmp_path / "live.service"
        generated.write_text(content)
        live.write_text(content)

        deployer = self._make_deployer(tmp_path)
        with caplog.at_level(logging.WARNING, logger="fraisier"):
            deployer._check_service_file_staleness_paths(generated, live)

        assert not any("out of sync" in r.message for r in caplog.records)

    def test_skips_when_generated_file_missing(self, tmp_path, caplog):
        """No warning when generated file does not exist (scaffold not run yet)."""
        import logging

        deployer = self._make_deployer(tmp_path)
        with caplog.at_level(logging.WARNING, logger="fraisier"):
            deployer._check_service_file_staleness_paths(
                tmp_path / "missing.service",
                tmp_path / "also-missing.service",
            )

        assert not any("out of sync" in r.message for r in caplog.records)

    def test_skips_when_no_systemd_service(self, tmp_path, caplog):
        """No-op when systemd_service is not configured."""
        import logging

        from fraisier.deployers.api import APIDeployer

        deployer = APIDeployer({"app_path": str(tmp_path)})
        with caplog.at_level(logging.WARNING, logger="fraisier"):
            deployer._check_service_file_staleness()

        assert not any("out of sync" in r.message for r in caplog.records)

    def test_generated_path_uses_state_dir(self, tmp_path, monkeypatch):
        """The staleness check compares against {state_dir}/systemd/... (#283)."""
        from fraisier.deployers.api import APIDeployer

        state_dir = tmp_path / "state"
        opt_config = tmp_path / "fraises.yaml"
        opt_config.write_text(
            f"name: myproject\nscaffold:\n  state_dir: {state_dir}\nfraises: {{}}\n"
        )
        monkeypatch.setenv("FRAISIER_CONFIG", str(opt_config))

        deployer = APIDeployer(
            {"app_path": str(tmp_path), "systemd_service": "myapp-api.service"}
        )
        with patch.object(
            deployer, "_check_service_file_staleness_paths"
        ) as mock_paths:
            deployer._check_service_file_staleness()

        generated = mock_paths.call_args.args[0]
        assert generated == state_dir / "systemd" / "myapp-api.service"


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
        assert result.error_message is not None
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


class TestSyncConfigHashPersistence:
    """save_hash() must be called after _regenerate_scaffold(), even when
    _install_scaffold() subsequently raises.  Without this, every deploy
    re-enters the regenerate+install loop indefinitely.
    """

    def _make_deployer(self, app_path):
        config = {
            "app_path": str(app_path),
            "systemd_service": "api.service",
        }
        return APIDeployer(config)

    def test_save_hash_persisted_even_when_install_fails(self, tmp_path):
        """save_hash() must be called even if _install_scaffold() raises."""
        from types import SimpleNamespace

        app_path = tmp_path / "app"
        app_path.mkdir()
        config_path = tmp_path / "fraises.yaml"
        config_path.write_text("project_name: test\n")

        saved = []

        def fake_save_hash():
            saved.append(True)

        def fake_regenerate(*, config_path):
            pass  # succeeds

        def fake_install(*, config_path=None):
            raise DeploymentError("sudo blocked")

        deployer = self._make_deployer(app_path)
        with (
            patch.object(deployer, "_regenerate_scaffold", side_effect=fake_regenerate),
            patch.object(deployer, "_install_scaffold", side_effect=fake_install),
            patch(
                "fraisier.config_watcher.ConfigWatcher",
                return_value=SimpleNamespace(
                    save_hash=fake_save_hash,
                    has_changed=lambda: True,
                ),
            ),
            patch.object(deployer, "_detect_config_changes", return_value=True),
            patch.object(deployer, "_sync_fraises_yaml"),
        ):
            # app_config must appear to exist so the branch is entered
            (app_path / "fraises.yaml").write_text("project_name: test\n")
            with pytest.raises(DeploymentError):
                deployer._sync_config_if_needed()

        assert saved, "save_hash() was not called despite successful regeneration"

    def test_second_deploy_skips_scaffold_when_hash_saved_after_failed_install(
        self, tmp_path
    ):
        """After a failed install that saved the hash, next deploy skips regen."""
        from types import SimpleNamespace

        app_path = tmp_path / "app"
        app_path.mkdir()
        (app_path / "fraises.yaml").write_text("project_name: test\n")

        regenerate_calls = []
        install_calls = []

        deployer = self._make_deployer(app_path)

        # Simulate: detect_config_changes returns False on second call (hash saved)
        detect_side_effects = [True, False]

        with (
            patch.object(
                deployer,
                "_detect_config_changes",
                side_effect=detect_side_effects,
            ),
            patch.object(deployer, "_sync_fraises_yaml"),
            patch.object(
                deployer,
                "_regenerate_scaffold",
                side_effect=lambda *, config_path=None: regenerate_calls.append(True),  # noqa: ARG005
            ),
            patch.object(
                deployer,
                "_install_scaffold",
                side_effect=lambda *, config_path=None: (  # noqa: ARG005
                    install_calls.append(True)
                    or (_ for _ in ()).throw(DeploymentError("blocked"))
                ),
            ),
            patch(
                "fraisier.config_watcher.ConfigWatcher",
                return_value=SimpleNamespace(
                    save_hash=lambda: None,
                    has_changed=lambda: True,
                ),
            ),
        ):
            # First deploy: regenerate+install, install fails, hash saved
            with pytest.raises(DeploymentError):
                deployer._sync_config_if_needed()

            assert len(regenerate_calls) == 1, "should regenerate on first deploy"

            # Second deploy: hash already saved → detect returns False → no regen
            deployer._sync_config_if_needed()

        assert len(regenerate_calls) == 1, "should NOT regenerate on unchanged config"
        assert len(install_calls) == 1, "should NOT install on unchanged config"

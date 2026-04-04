"""Unit tests for ServerBootstrapper."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from fraisier.bootstrap import (
    BootstrapResult,
    ServerBootstrapper,
    StepResult,
    resolve_become_password,
)
from fraisier.config import FraisierConfig
from fraisier.runners import SSHRunner

_MINIMAL_YAML = """\
name: myapp
fraises:
  api:
    type: api
    environments:
      production:
        name: myapp-api-prod
        server: prod.example.com
      staging:
        name: myapp-api-staging
        server: staging.example.com
scaffold:
  deploy_user: myapp_deploy
"""

_OK = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _err(msg: str = "error") -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, "cmd", stderr=msg)


@pytest.fixture
def config(tmp_path):
    p = tmp_path / "fraises.yaml"
    p.write_text(_MINIMAL_YAML)
    return FraisierConfig(p)


@pytest.fixture
def mock_runner():
    runner = MagicMock(spec=SSHRunner)
    runner.run.return_value = _OK
    return runner


@pytest.fixture
def bootstrapper(config, mock_runner, tmp_path):
    return ServerBootstrapper(
        config=config,
        environment="production",
        runner=mock_runner,
        fraises_yaml_path=tmp_path / "fraises.yaml",
    )


@pytest.fixture
def dry_bootstrapper(config, mock_runner, tmp_path):
    return ServerBootstrapper(
        config=config,
        environment="production",
        runner=mock_runner,
        fraises_yaml_path=tmp_path / "fraises.yaml",
        dry_run=True,
    )


# ---------------------------------------------------------------------------
# StepResult / BootstrapResult
# ---------------------------------------------------------------------------


class TestStepResult:
    def test_success_defaults(self):
        step = StepResult(name="x", success=True)
        assert step.already_done is False
        assert step.error == ""
        assert step.command == ""

    def test_failure_carries_error(self):
        step = StepResult(name="x", success=False, error="boom")
        assert step.success is False
        assert step.error == "boom"


class TestBootstrapResult:
    def test_success_when_all_pass(self):
        r = BootstrapResult(
            steps=[
                StepResult(name="a", success=True),
                StepResult(name="b", success=True),
            ]
        )
        assert r.success is True

    def test_failure_when_any_fails(self):
        r = BootstrapResult(
            steps=[
                StepResult(name="a", success=True),
                StepResult(name="b", success=False),
            ]
        )
        assert r.success is False

    def test_failed_step_returns_first_failure(self):
        r = BootstrapResult(
            steps=[
                StepResult(name="a", success=True),
                StepResult(name="b", success=False, error="boom"),
                StepResult(name="c", success=False),
            ]
        )
        assert r.failed_step is not None
        assert r.failed_step.name == "b"

    def test_failed_step_none_when_all_pass(self):
        r = BootstrapResult(steps=[StepResult(name="a", success=True)])
        assert r.failed_step is None


# ---------------------------------------------------------------------------
# _run_remote
# ---------------------------------------------------------------------------


class TestRunRemote:
    def test_success_returns_step(self, bootstrapper, mock_runner):
        step = bootstrapper._run_remote("my step", ["echo", "hi"])
        assert step.success is True
        assert step.name == "my step"

    def test_failure_returns_failed_step(self, bootstrapper, mock_runner):
        mock_runner.run.side_effect = _err("permission denied")
        step = bootstrapper._run_remote("bad step", ["false"])
        assert step.success is False
        assert "permission denied" in step.error

    def test_already_done_skips_main_cmd(self, bootstrapper, mock_runner):
        """When already_done_cmd succeeds, main command is not called."""
        step = bootstrapper._run_remote(
            "x", ["main-cmd"], already_done_cmd=["check-cmd"]
        )
        assert step.success is True
        assert step.already_done is True
        mock_runner.run.assert_called_once()
        assert mock_runner.run.call_args[0][0] == ["check-cmd"]

    def test_already_done_cmd_failure_runs_main(self, bootstrapper, mock_runner):
        """When already_done_cmd fails, main command runs."""
        mock_runner.run.side_effect = [_err(), _OK]
        step = bootstrapper._run_remote(
            "x", ["main-cmd"], already_done_cmd=["check-cmd"]
        )
        assert step.success is True
        assert step.already_done is False
        assert mock_runner.run.call_count == 2

    def test_dry_run_does_not_call_runner(self, dry_bootstrapper, mock_runner):
        step = dry_bootstrapper._run_remote("step", ["some", "cmd"])
        assert step.success is True
        mock_runner.run.assert_not_called()

    def test_dry_run_records_command(self, dry_bootstrapper):
        step = dry_bootstrapper._run_remote("step", ["some", "cmd"])
        assert "some" in step.command and "cmd" in step.command


# ---------------------------------------------------------------------------
# Individual step methods
# ---------------------------------------------------------------------------


class TestCreateDeployUser:
    def test_skips_when_user_exists(self, bootstrapper, mock_runner):
        step = bootstrapper._create_deploy_user()
        assert step.success is True
        assert step.already_done is True

    def test_creates_user_when_missing(self, bootstrapper, mock_runner):
        mock_runner.run.side_effect = [_err(), _OK]
        step = bootstrapper._create_deploy_user()
        assert step.success is True
        create_call = mock_runner.run.call_args[0][0]
        assert "useradd" in create_call

    def test_propagates_useradd_failure(self, bootstrapper, mock_runner):
        mock_runner.run.side_effect = [_err(), _err("useradd: permission denied")]
        step = bootstrapper._create_deploy_user()
        assert step.success is False
        assert "useradd: permission denied" in step.error


class TestAddToWwwData:
    def test_runs_usermod(self, bootstrapper, mock_runner):
        step = bootstrapper._add_to_www_data()
        assert step.success is True
        cmd = mock_runner.run.call_args[0][0]
        assert "usermod" in cmd
        assert "-aG" in cmd
        assert "www-data" in cmd
        assert "myapp_deploy" in cmd

    def test_failure_returns_failed_step(self, bootstrapper, mock_runner):
        mock_runner.run.side_effect = _err("usermod failed")
        step = bootstrapper._add_to_www_data()
        assert step.success is False


class TestInstallUv:
    def test_skips_when_uv_exists(self, bootstrapper, mock_runner):
        step = bootstrapper._install_uv()
        assert step.success is True
        assert step.already_done is True

    def test_installs_when_missing(self, bootstrapper, mock_runner):
        mock_runner.run.side_effect = [_err(), _OK]
        step = bootstrapper._install_uv()
        assert step.success is True
        install_cmd = mock_runner.run.call_args[0][0]
        assert any("uv" in part for part in install_cmd)

    def test_uv_check_uses_deploy_user_home(self, bootstrapper, mock_runner):
        bootstrapper._install_uv()
        check_cmd = mock_runner.run.call_args[0][0]
        assert any("myapp_deploy" in part for part in check_cmd)


class TestInstallFraisier:
    def test_always_installs_pinned_version(self, bootstrapper, mock_runner):
        from fraisier import __version__

        step = bootstrapper._install_fraisier()
        assert step.success is True
        install_cmd = mock_runner.run.call_args[0][0]
        cmd_str = " ".join(install_cmd)
        assert "--force" in cmd_str
        assert f"fraisier=={__version__}" in cmd_str

    def test_failure_returns_failed_step(self, bootstrapper, mock_runner):
        mock_runner.run.side_effect = _err("permission denied")
        step = bootstrapper._install_fraisier()
        assert step.success is False


class TestCreateDirectories:
    def test_creates_expected_dirs(self, bootstrapper, mock_runner):
        step = bootstrapper._create_directories()
        assert step.success is True
        cmd = mock_runner.run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "mkdir" in cmd_str
        assert "/opt/myapp" in cmd_str
        assert "/opt/fraisier" in cmd_str
        assert "/run/fraisier" in cmd_str


class TestUploadConfig:
    def test_dry_run_no_io(self, dry_bootstrapper, mock_runner):
        step = dry_bootstrapper._upload_config()
        assert step.success is True
        mock_runner.run.assert_not_called()
        mock_runner.upload.assert_not_called()

    def test_calls_mkdir_and_upload(self, bootstrapper, mock_runner):
        step = bootstrapper._upload_config()
        assert step.success is True
        # First call: mkdir -p /opt/fraisier
        mkdir_call = mock_runner.run.call_args_list[0]
        assert "mkdir" in mkdir_call[0][0]
        # Then upload
        mock_runner.upload.assert_called_once()
        upload_args = mock_runner.upload.call_args[0]
        assert upload_args[1] == "/opt/fraisier/fraises.yaml"

    def test_upload_failure_returns_failed_step(self, bootstrapper, mock_runner):
        mock_runner.upload.side_effect = subprocess.CalledProcessError(
            1, "scp", stderr="connection refused"
        )
        step = bootstrapper._upload_config()
        assert step.success is False
        assert "connection refused" in step.error


class TestUploadScaffoldFiles:
    def test_dry_run_no_io(self, dry_bootstrapper, mock_runner):
        step, remote_dir = dry_bootstrapper._upload_scaffold_files()
        assert step.success is True
        assert remote_dir == "/tmp/fraisier-bootstrap"
        mock_runner.upload_tree.assert_not_called()

    def test_renders_and_uploads(self, bootstrapper, mock_runner, tmp_path):
        with patch("fraisier.scaffold.renderer.ScaffoldRenderer") as mock_renderer_cls:
            mock_renderer = MagicMock()
            mock_renderer_cls.return_value = mock_renderer
            step, _remote_dir = bootstrapper._upload_scaffold_files()

        assert step.success is True
        mock_renderer.render.assert_called_once()
        mock_runner.upload_tree.assert_called_once()

    def test_upload_tree_failure_returns_failed_step(self, bootstrapper, mock_runner):
        mock_runner.upload_tree.side_effect = subprocess.CalledProcessError(
            1, "scp", stderr="upload failed"
        )
        with patch("fraisier.scaffold.renderer.ScaffoldRenderer"):
            step, _ = bootstrapper._upload_scaffold_files()
        assert step.success is False
        assert "upload failed" in step.error


class TestRunInstall:
    def test_runs_install_sh_standalone(self, bootstrapper, mock_runner):
        step = bootstrapper._run_install("/tmp/fraisier-bootstrap")
        assert step.success is True
        cmd = mock_runner.run.call_args[0][0]
        assert "--standalone" in cmd
        assert "--scaffold-dir" in cmd
        assert "/tmp/fraisier-bootstrap" in cmd

    def test_verbose_adds_verbose_flag(self, config, mock_runner, tmp_path):
        b = ServerBootstrapper(
            config=config,
            environment="production",
            runner=mock_runner,
            fraises_yaml_path=tmp_path / "fraises.yaml",
            verbose=True,
        )
        b._run_install("/tmp/x")
        cmd = mock_runner.run.call_args[0][0]
        assert "--verbose" in cmd


class TestEnableSockets:
    def test_enables_correct_socket_unit(self, bootstrapper, mock_runner):
        step = bootstrapper._enable_sockets()
        assert step.success is True
        cmd = mock_runner.run.call_args[0][0]
        assert "systemctl" in cmd
        assert "enable" in cmd
        assert "fraisier-myapp-api-prod.socket" in cmd

    def test_uses_environment_name_field_in_socket_name(
        self, config, mock_runner, tmp_path
    ):
        b = ServerBootstrapper(
            config=config,
            environment="staging",
            runner=mock_runner,
            fraises_yaml_path=tmp_path / "fraises.yaml",
        )
        b._enable_sockets()
        cmd = mock_runner.run.call_args[0][0]
        assert "fraisier-myapp-api-staging.socket" in cmd

    def test_fails_when_environment_has_no_fraises(self, config, mock_runner, tmp_path):
        b = ServerBootstrapper(
            config=config,
            environment="nonexistent",
            runner=mock_runner,
            fraises_yaml_path=tmp_path / "fraises.yaml",
        )
        step = b._enable_sockets()
        assert step.success is False
        assert "nonexistent" in step.error


_TWO_FRAISE_YAML = """\
name: myapp
fraises:
  api:
    type: api
    environments:
      production:
        name: myapp-api-prod
        server: prod.example.com
  worker:
    type: worker
    environments:
      production:
        name: myapp-worker-prod
        server: prod.example.com
scaffold:
  deploy_user: myapp_deploy
"""


class TestValidate:
    def test_calls_fraisier_validate_setup(self, bootstrapper, mock_runner):
        step = bootstrapper._validate()
        assert step.success is True
        cmd = mock_runner.run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "fraisier" in cmd_str
        assert "validate-setup" in cmd_str
        assert "/opt/fraisier/fraises.yaml" in cmd_str
        assert "api" in cmd_str
        assert "production" in cmd_str

    def test_uses_deploy_user(self, bootstrapper, mock_runner):
        bootstrapper._validate()
        cmd = mock_runner.run.call_args[0][0]
        assert "myapp_deploy" in cmd

    def test_iterates_all_fraises_for_environment(self, mock_runner, tmp_path):
        p = tmp_path / "fraises.yaml"
        p.write_text(_TWO_FRAISE_YAML)
        bootstrapper = ServerBootstrapper(
            config=FraisierConfig(p),
            environment="production",
            runner=mock_runner,
            fraises_yaml_path=p,
        )
        step = bootstrapper._validate()
        assert step.success is True
        assert mock_runner.run.call_count == 2
        calls = [" ".join(c[0][0]) for c in mock_runner.run.call_args_list]
        assert any("api" in s for s in calls)
        assert any("worker" in s for s in calls)
        assert all("production" in s for s in calls)

    def test_returns_error_if_no_fraises_for_environment(self, mock_runner, tmp_path):
        p = tmp_path / "fraises.yaml"
        p.write_text(_MINIMAL_YAML)
        bootstrapper = ServerBootstrapper(
            config=FraisierConfig(p),
            environment="nonexistent",
            runner=mock_runner,
            fraises_yaml_path=p,
        )
        step = bootstrapper._validate()
        assert step.success is False
        assert "nonexistent" in step.error
        mock_runner.run.assert_not_called()

    def test_stops_at_first_failing_fraise(self, mock_runner, tmp_path):
        p = tmp_path / "fraises.yaml"
        p.write_text(_TWO_FRAISE_YAML)
        bootstrapper = ServerBootstrapper(
            config=FraisierConfig(p),
            environment="production",
            runner=mock_runner,
            fraises_yaml_path=p,
        )
        mock_runner.run.side_effect = [_err("validate failed"), _OK]
        step = bootstrapper._validate()
        assert step.success is False
        assert mock_runner.run.call_count == 1


# ---------------------------------------------------------------------------
# Full bootstrap() integration
# ---------------------------------------------------------------------------


class TestBootstrapFlow:
    def test_dry_run_succeeds_without_any_runner_calls(
        self, dry_bootstrapper, mock_runner
    ):
        result = dry_bootstrapper.bootstrap()
        assert result.success is True
        mock_runner.run.assert_not_called()
        mock_runner.upload.assert_not_called()
        mock_runner.upload_tree.assert_not_called()

    def test_dry_run_produces_ten_steps(self, dry_bootstrapper):
        result = dry_bootstrapper.bootstrap()
        assert len(result.steps) == 10

    def test_aborts_after_first_failure(self, bootstrapper, mock_runner):
        # Make _add_to_www_data fail (step 2); steps after it must not run.
        # Step 1 check: already done (ok). Step 2: fails.
        mock_runner.run.side_effect = [
            _OK,  # create_deploy_user: already_done_cmd succeeds
            _err("usermod"),  # add_to_www_data: fails
        ]
        result = bootstrapper.bootstrap()
        assert result.success is False
        assert len(result.steps) == 2
        assert result.failed_step is not None
        assert result.failed_step.name == "Add deploy user to www-data"

    def test_cleanup_called_on_late_failure(self, bootstrapper, mock_runner):
        """Cleanup runs even when a post-upload step fails."""

        def _ok(name: str) -> StepResult:
            return StepResult(name=name, success=True)

        _fail = StepResult(name="8", success=False, error="bash: not found")

        with (
            patch.object(bootstrapper, "_create_deploy_user", return_value=_ok("1")),
            patch.object(bootstrapper, "_add_to_www_data", return_value=_ok("2")),
            patch.object(bootstrapper, "_install_uv", return_value=_ok("3")),
            patch.object(bootstrapper, "_install_fraisier", return_value=_ok("4")),
            patch.object(bootstrapper, "_create_directories", return_value=_ok("5")),
            patch.object(bootstrapper, "_upload_config", return_value=_ok("6")),
            patch.object(
                bootstrapper,
                "_upload_scaffold_files",
                return_value=(_ok("7"), "/tmp/x"),
            ),
            patch.object(bootstrapper, "_run_install", return_value=_fail),
            patch.object(bootstrapper, "_cleanup") as mock_cleanup,
        ):
            result = bootstrapper.bootstrap()

        assert result.success is False
        mock_cleanup.assert_called_once_with("/tmp/x")


# ---------------------------------------------------------------------------
# resolve_become_password
# ---------------------------------------------------------------------------


class TestResolveBecomePassword:
    def test_captures_stdout(self):
        password = resolve_become_password("echo hunter2")
        assert password == "hunter2"

    def test_strips_trailing_newline(self):
        password = resolve_become_password("printf 'secret\\n'")
        assert password == "secret"

    def test_strips_trailing_whitespace(self):
        password = resolve_become_password("printf 'secret  \\n'")
        assert password == "secret"

    def test_raises_on_nonzero_exit(self):
        with pytest.raises(RuntimeError, match="become_password_command failed"):
            resolve_become_password("false")

    def test_raises_includes_stderr(self):
        with pytest.raises(RuntimeError, match="not found"):
            resolve_become_password("echo 'not found' >&2 && exit 1")

    def test_supports_pipe_commands(self):
        password = resolve_become_password("echo 'hello world' | cut -d' ' -f2")
        assert password == "world"

    def test_empty_output_returns_empty_string(self):
        password = resolve_become_password("printf ''")
        assert password == ""

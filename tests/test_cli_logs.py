"""Tests for logs command."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.logs import _build_ssh_cmd, _resolve_unit_pattern
from fraisier.cli.main import main


class TestResolveUnitPattern:
    """Unit patterns must match what the scaffold actually installs."""

    def _config(self, project_name: str = "myproject") -> MagicMock:
        c = MagicMock()
        c.project_name = project_name
        return c

    # --- deploy daemon ---

    def test_deploy_pattern_with_name_field(self):
        # env has name: → socket is fraisier-{name}.socket → service is fraisier-{name}@*.service
        env_config = {"name": "api.myapp.dev"}
        pattern = _resolve_unit_pattern(
            self._config(), "api", "development", env_config, "deploy"
        )
        assert pattern == "fraisier-api.myapp.dev@*.service"

    def test_deploy_pattern_fallback_fraise_env(self):
        # no name: → fraisier-{fraise}-{env}@*.service
        env_config = {"app_path": "/opt/api"}
        pattern = _resolve_unit_pattern(
            self._config(), "api", "production", env_config, "deploy"
        )
        assert pattern == "fraisier-api-production@*.service"

    def test_deploy_pattern_explicit_socket_override(self):
        env_config = {"systemd_deploy_socket": "custom-deploy.socket"}
        pattern = _resolve_unit_pattern(
            self._config(), "api", "production", env_config, "deploy"
        )
        assert pattern == "custom-deploy@*.service"

    # --- app service ---

    def test_app_pattern_default(self):
        # no overrides → {project}_{fraise}_{env}.service
        env_config = {}
        pattern = _resolve_unit_pattern(
            self._config("proj"), "api", "production", env_config, "app"
        )
        assert pattern == "proj_api_production.service"

    def test_app_pattern_with_systemd_service(self):
        env_config = {"systemd_service": "api.myapp.dev.service"}
        pattern = _resolve_unit_pattern(
            self._config(), "api", "development", env_config, "app"
        )
        assert pattern == "api.myapp.dev.service"

    def test_app_pattern_with_service_name_override(self):
        env_config = {"service": {"service_name": "myapp-api"}}
        pattern = _resolve_unit_pattern(
            self._config(), "api", "production", env_config, "app"
        )
        assert pattern == "myapp-api.service"

    def test_deploy_and_app_patterns_differ(self):
        env_config = {"name": "api.myapp.io"}
        deploy = _resolve_unit_pattern(
            self._config(), "api", "production", env_config, "deploy"
        )
        app = _resolve_unit_pattern(
            self._config(), "api", "production", env_config, "app"
        )
        assert deploy != app
        assert "@*.service" in deploy
        assert "@" not in app


class TestBuildSshCmd:
    """Tests for _build_ssh_cmd helper."""

    def test_minimal_config(self):
        cmd = _build_ssh_cmd({"host": "example.com"})
        assert cmd[0] == "ssh"
        assert "root@example.com" in cmd
        assert "-p" in cmd
        assert cmd[cmd.index("-p") + 1] == "22"

    def test_custom_user_port(self):
        cmd = _build_ssh_cmd({"host": "example.com", "user": "deploy", "port": 2222})
        assert "deploy@example.com" in cmd
        assert cmd[cmd.index("-p") + 1] == "2222"

    def test_key_path_included(self):
        cmd = _build_ssh_cmd(
            {"host": "example.com", "key_path": "/home/user/.ssh/id_rsa"}
        )
        assert "-i" in cmd
        assert cmd[cmd.index("-i") + 1] == "/home/user/.ssh/id_rsa"

    def test_no_key_path_when_absent(self):
        cmd = _build_ssh_cmd({"host": "example.com"})
        assert "-i" not in cmd

    def test_strict_host_key_default(self):
        cmd = _build_ssh_cmd({"host": "example.com"})
        opts = " ".join(cmd)
        assert "StrictHostKeyChecking=accept-new" in opts

    def test_strict_host_key_disabled(self):
        cmd = _build_ssh_cmd({"host": "example.com", "strict_host_key": False})
        opts = " ".join(cmd)
        assert "StrictHostKeyChecking=no" in opts

    def test_batch_mode_always_set(self):
        cmd = _build_ssh_cmd({"host": "example.com"})
        opts = " ".join(cmd)
        assert "BatchMode=yes" in opts


class TestLogsCommand:
    """Integration tests for the logs CLI command."""

    def _make_config(self, project_name="myproject", ssh_config=None, env_name=None):
        config = MagicMock()
        config.project_name = project_name
        fraise_env = {"type": "api"}
        if env_name:
            fraise_env["name"] = env_name
        if ssh_config:
            fraise_env["ssh"] = ssh_config
        config.get_fraise_environment.return_value = fraise_env
        return config

    @patch("fraisier.cli.logs.os.execvp")
    def test_local_follow_calls_journalctl(self, mock_execvp):
        runner = CliRunner()
        # env has name: → socket fraisier-api.myapp.dev.socket → service fraisier-api.myapp.dev@*.service
        config = self._make_config(env_name="api.myapp.dev")
        with patch("fraisier.cli.main.get_config", return_value=config):
            runner.invoke(
                main,
                ["logs", "api", "production"],
                obj={"config": config, "skip_health": False},
            )
        prog, args = mock_execvp.call_args[0]
        assert prog == "journalctl"
        assert "-f" in args
        assert "fraisier-api.myapp.dev@*.service" in args

    @patch("fraisier.cli.logs.os.execvp")
    def test_local_follow_fallback_pattern(self, mock_execvp):
        # no name: in env_config → fraisier-{fraise}-{env}@*.service
        runner = CliRunner()
        config = self._make_config()
        with patch("fraisier.cli.main.get_config", return_value=config):
            runner.invoke(
                main,
                ["logs", "api", "production"],
                obj={"config": config, "skip_health": False},
            )
        _, args = mock_execvp.call_args[0]
        assert "fraisier-api-production@*.service" in args

    @patch("fraisier.cli.logs.os.execvp")
    def test_local_no_follow(self, mock_execvp):
        runner = CliRunner()
        config = self._make_config()
        with patch("fraisier.cli.main.get_config", return_value=config):
            runner.invoke(
                main,
                ["logs", "api", "production", "--no-follow", "--lines", "100"],
                obj={"config": config, "skip_health": False},
            )
        prog, args = mock_execvp.call_args[0]
        assert prog == "journalctl"
        assert "-f" not in args
        assert "100" in args

    @patch("fraisier.cli.logs.os.execvp")
    def test_local_since_flag(self, mock_execvp):
        runner = CliRunner()
        config = self._make_config()
        with patch("fraisier.cli.main.get_config", return_value=config):
            runner.invoke(
                main,
                ["logs", "api", "production", "--no-follow", "--since", "1 hour ago"],
                obj={"config": config, "skip_health": False},
            )
        _, args = mock_execvp.call_args[0]
        assert "--since" in args
        assert "1 hour ago" in args

    @patch("fraisier.cli.logs.os.execvp")
    def test_local_app_service_pattern(self, mock_execvp):
        runner = CliRunner()
        config = self._make_config(project_name="proj")
        with patch("fraisier.cli.main.get_config", return_value=config):
            runner.invoke(
                main,
                ["logs", "api", "production", "--service", "app"],
                obj={"config": config, "skip_health": False},
            )
        _, args = mock_execvp.call_args[0]
        assert "proj_api_production.service" in args
        assert "@" not in " ".join(args)

    @patch("fraisier.cli.logs.os.execvp")
    def test_remote_calls_ssh_not_journalctl(self, mock_execvp):
        runner = CliRunner()
        config = self._make_config(ssh_config={"host": "prod.example.com"})
        with patch("fraisier.cli.main.get_config", return_value=config):
            runner.invoke(
                main,
                ["logs", "api", "production"],
                obj={"config": config, "skip_health": False},
            )
        prog, args = mock_execvp.call_args[0]
        assert prog == "ssh"
        assert args[0] == "ssh"

    @patch("fraisier.cli.logs.os.execvp")
    def test_remote_targets_correct_host(self, mock_execvp):
        runner = CliRunner()
        config = self._make_config(
            ssh_config={"host": "prod.example.com", "user": "deploy"}
        )
        with patch("fraisier.cli.main.get_config", return_value=config):
            runner.invoke(
                main,
                ["logs", "api", "production"],
                obj={"config": config, "skip_health": False},
            )
        _, args = mock_execvp.call_args[0]
        assert "deploy@prod.example.com" in args

    @patch("fraisier.cli.logs.os.execvp")
    def test_remote_journalctl_args_in_ssh_command(self, mock_execvp):
        runner = CliRunner()
        config = self._make_config(
            env_name="api.myapp.io", ssh_config={"host": "prod.example.com"}
        )
        with patch("fraisier.cli.main.get_config", return_value=config):
            runner.invoke(
                main,
                ["logs", "api", "production", "--no-follow", "--lines", "20"],
                obj={"config": config, "skip_health": False},
            )
        _, args = mock_execvp.call_args[0]
        remote_cmd = args[-1]
        assert "journalctl" in remote_cmd
        assert "fraisier-api.myapp.io@*.service" in remote_cmd
        assert "-f" not in remote_cmd
        assert "20" in remote_cmd

    @patch("fraisier.cli.logs.os.execvp")
    def test_remote_follow_mode_includes_follow_flag(self, mock_execvp):
        runner = CliRunner()
        config = self._make_config(ssh_config={"host": "prod.example.com"})
        with patch("fraisier.cli.main.get_config", return_value=config):
            runner.invoke(
                main,
                ["logs", "api", "production"],
                obj={"config": config, "skip_health": False},
            )
        _, args = mock_execvp.call_args[0]
        remote_cmd = args[-1]
        assert "-f" in remote_cmd

    @patch("fraisier.cli.logs.os.execvp")
    def test_remote_app_service(self, mock_execvp):
        runner = CliRunner()
        config = self._make_config(
            project_name="proj", ssh_config={"host": "prod.example.com"}
        )
        with patch("fraisier.cli.main.get_config", return_value=config):
            runner.invoke(
                main,
                ["logs", "api", "production", "--service", "app"],
                obj={"config": config, "skip_health": False},
            )
        _, args = mock_execvp.call_args[0]
        remote_cmd = args[-1]
        assert "proj_api_production.service" in remote_cmd

    def test_invalid_fraise_shows_error(self):
        runner = CliRunner()
        config = MagicMock()
        config.get_fraise_environment.return_value = None
        with patch("fraisier.cli.main.get_config", return_value=config):
            result = runner.invoke(
                main,
                ["logs", "invalid", "fraise"],
                obj={"config": config, "skip_health": False},
            )
        assert result.exit_code == 1
        assert "not found" in result.output


class TestSshConfigValidation:
    """Config loader validates the ssh: block at load time."""

    _BASE_YAML = """\
project:
  name: proj
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /opt/api
        clone_url: git@github.com:org/repo.git
"""

    def _load(self, tmp_path, ssh_yaml: str):
        from fraisier.config.loader import FraisierConfig

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(self._BASE_YAML + "        ssh:\n" + ssh_yaml)
        return FraisierConfig(str(cfg))

    def test_valid_ssh_block_passes(self, tmp_path):
        self._load(
            tmp_path,
            "          host: prod.example.com\n"
            "          user: deploy\n"
            "          port: 22\n"
            "          key_path: /home/deploy/.ssh/id_rsa\n"
            "          strict_host_key: true\n",
        )  # no exception

    def test_minimal_ssh_block_passes(self, tmp_path):
        self._load(tmp_path, "          host: prod.example.com\n")  # no exception

    def test_missing_host_raises(self, tmp_path):
        from fraisier.errors import ValidationError

        with pytest.raises(ValidationError, match=r"ssh\.host is required"):
            self._load(tmp_path, "          user: deploy\n")

    def test_non_dict_ssh_raises(self, tmp_path):
        from fraisier.errors import ValidationError

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(self._BASE_YAML + "        ssh: prod.example.com\n")
        from fraisier.config.loader import FraisierConfig

        with pytest.raises(ValidationError, match="'ssh' must be a mapping"):
            FraisierConfig(str(cfg))

    def test_invalid_port_type_raises(self, tmp_path):
        from fraisier.errors import ValidationError

        with pytest.raises(ValidationError, match=r"ssh\.port must be an integer"):
            self._load(
                tmp_path,
                "          host: prod.example.com\n          port: '22'\n",
            )

    def test_invalid_strict_host_key_type_raises(self, tmp_path):
        from fraisier.errors import ValidationError

        with pytest.raises(
            ValidationError, match=r"ssh\.strict_host_key must be a boolean"
        ):
            self._load(
                tmp_path,
                "          host: prod.example.com\n          strict_host_key: 'yes'\n",
            )

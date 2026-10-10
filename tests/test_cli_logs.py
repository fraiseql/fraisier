"""Tests for logs command."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.logs import _resolve_unit_pattern
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
        # A real fraise dict: a bare MagicMock reads as serving only by accident.
        config.get_fraise.return_value = {
            "type": "api",
            "environments": {"production": dict(fraise_env)},
        }
        return config

    def _mock_popen(self):
        """Return a Popen mock whose .wait() returns 0."""
        proc = MagicMock()
        proc.wait.return_value = 0
        proc.returncode = 0
        mock = MagicMock(return_value=proc)
        return mock

    def _invoke(self, config, args):
        runner = CliRunner()
        mock_popen = self._mock_popen()
        # Local journalctl spawns Popen directly from cli.logs; remote
        # routes through fraisier.ssh.long_stream which spawns from
        # fraisier.ssh. Patch both so the test mock catches either path.
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli.logs.subprocess.Popen", mock_popen),
            patch("fraisier.ssh.subprocess.Popen", mock_popen),
            patch("fraisier.cli.logs.sys.exit"),
        ):
            runner.invoke(main, args, obj={"config": config, "skip_health": False})
        return mock_popen

    def test_local_follow_calls_journalctl(self):
        # env has name: → socket fraisier-api.myapp.dev.socket → service fraisier-api.myapp.dev@*.service
        config = self._make_config(env_name="api.myapp.dev")
        mock_popen = self._invoke(config, ["logs", "api", "production"])
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "journalctl"
        assert "-f" in cmd
        assert "fraisier-api.myapp.dev@*.service" in cmd

    def test_local_follow_fallback_pattern(self):
        # no name: in env_config → fraisier-{fraise}-{env}@*.service
        config = self._make_config()
        mock_popen = self._invoke(config, ["logs", "api", "production"])
        cmd = mock_popen.call_args[0][0]
        assert "fraisier-api-production@*.service" in cmd

    def test_local_no_follow(self):
        config = self._make_config()
        mock_popen = self._invoke(
            config, ["logs", "api", "production", "--no-follow", "--lines", "100"]
        )
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "journalctl"
        assert "-f" not in cmd
        assert "100" in cmd

    def test_local_since_flag(self):
        config = self._make_config()
        mock_popen = self._invoke(
            config,
            ["logs", "api", "production", "--no-follow", "--since", "1 hour ago"],
        )
        cmd = mock_popen.call_args[0][0]
        assert "--since" in cmd
        assert "1 hour ago" in cmd

    def test_local_app_service_pattern(self):
        config = self._make_config(project_name="proj")
        mock_popen = self._invoke(
            config, ["logs", "api", "production", "--service", "app"]
        )
        cmd = mock_popen.call_args[0][0]
        assert "proj_api_production.service" in cmd
        assert "@" not in " ".join(cmd)

    def test_remote_calls_ssh_not_journalctl(self):
        config = self._make_config(ssh_config={"host": "prod.example.com"})
        mock_popen = self._invoke(config, ["logs", "api", "production"])
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "ssh"

    def test_remote_targets_correct_host(self):
        config = self._make_config(
            ssh_config={"host": "prod.example.com", "user": "deploy"}
        )
        mock_popen = self._invoke(config, ["logs", "api", "production"])
        cmd = mock_popen.call_args[0][0]
        assert "deploy@prod.example.com" in cmd

    def test_remote_journalctl_args_in_ssh_command(self):
        config = self._make_config(
            env_name="api.myapp.io", ssh_config={"host": "prod.example.com"}
        )
        mock_popen = self._invoke(
            config, ["logs", "api", "production", "--no-follow", "--lines", "20"]
        )
        cmd = mock_popen.call_args[0][0]
        remote_cmd = cmd[-1]  # last element is the remote command string
        assert "journalctl" in remote_cmd
        assert "fraisier-api.myapp.io@*.service" in remote_cmd
        assert "-f" not in remote_cmd
        assert "20" in remote_cmd

    def test_remote_follow_mode_includes_follow_flag(self):
        config = self._make_config(ssh_config={"host": "prod.example.com"})
        mock_popen = self._invoke(config, ["logs", "api", "production"])
        cmd = mock_popen.call_args[0][0]
        remote_cmd = cmd[-1]
        assert "-f" in remote_cmd

    def test_remote_app_service(self):
        config = self._make_config(
            project_name="proj", ssh_config={"host": "prod.example.com"}
        )
        mock_popen = self._invoke(
            config, ["logs", "api", "production", "--service", "app"]
        )
        cmd = mock_popen.call_args[0][0]
        remote_cmd = cmd[-1]
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
        from tests._eager_load import eager_load

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(self._BASE_YAML + "        ssh:\n" + ssh_yaml)
        return eager_load(str(cfg))

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
        from tests._eager_load import eager_load

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(self._BASE_YAML + "        ssh: prod.example.com\n")

        with pytest.raises(ValidationError, match="'ssh' must be a mapping"):
            eager_load(str(cfg))

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

    def test_invalid_connect_timeout_type_raises(self, tmp_path):
        from fraisier.errors import ValidationError

        with pytest.raises(
            ValidationError, match=r"ssh\.connect_timeout must be an integer"
        ):
            self._load(
                tmp_path,
                "          host: prod.example.com\n          connect_timeout: '30'\n",
            )

    def test_valid_connect_timeout_passes(self, tmp_path):
        self._load(
            tmp_path,
            "          host: prod.example.com\n          connect_timeout: 10\n",
        )  # no exception

    def test_invalid_address_family_raises(self, tmp_path):
        from fraisier.errors import ValidationError

        with pytest.raises(
            ValidationError,
            match=r"ssh\.address_family must be 'inet', 'inet6', or 'any'",
        ):
            self._load(
                tmp_path,
                "          host: prod.example.com\n          address_family: ipv4\n",
            )

    def test_valid_address_family_inet_passes(self, tmp_path):
        self._load(
            tmp_path,
            "          host: prod.example.com\n          address_family: inet\n",
        )  # no exception

    def test_valid_address_family_any_passes(self, tmp_path):
        self._load(
            tmp_path,
            "          host: prod.example.com\n          address_family: any\n",
        )  # no exception


_ROLES_CONFIG = """\
name: proj
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /srv/api
  stats:
    type: scheduled
    environments:
      production:
        app_path: /srv/api
        jobs:
          daily:
            systemd_service: proj-daily-stats.service
            systemd_timer: proj-daily-stats.timer
          weekly:
            systemd_service: proj-weekly-report.service
  nightly:
    type: scheduled
    environments:
      production:
        app_path: /srv/api
        systemd_service: nightly.service
        systemd_timer: nightly.timer
  dumps:
    type: backup
    environments:
      production:
        app_path: /srv/api
"""


class TestAppLogsForAFraiseThatServesNothing:
    """--service app on a fraise with no app unit says so (#449)."""

    def _invoke(self, tmp_path, args):
        from fraisier.config import FraisierConfig

        path = tmp_path / "fraises.yaml"
        path.write_text(_ROLES_CONFIG)
        config = FraisierConfig(str(path))
        popen = MagicMock()
        popen.return_value.wait.return_value = 0
        popen.return_value.returncode = 0
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli.logs.subprocess.Popen", popen),
        ):
            result = CliRunner().invoke(
                main, args, obj={"config": config, "skip_health": False}
            )
        return result, popen

    def test_a_jobs_shaped_fraise_names_its_job_units(self, tmp_path):
        result, popen = self._invoke(
            tmp_path, ["logs", "stats", "production", "--service", "app"]
        )
        assert result.exit_code != 0
        popen.assert_not_called()
        assert "serves nothing" in result.output
        assert "proj-daily-stats.service" in result.output
        assert "proj-weekly-report.service" in result.output
        assert "--service deploy" in result.output

    def test_a_flat_scheduled_fraise_names_its_unit(self, tmp_path):
        result, popen = self._invoke(
            tmp_path, ["logs", "nightly", "production", "--service", "app"]
        )
        assert result.exit_code != 0
        popen.assert_not_called()
        assert "nightly.service" in result.output

    def test_a_fraise_with_no_units_points_at_the_deploy_logs(self, tmp_path):
        result, popen = self._invoke(
            tmp_path, ["logs", "dumps", "production", "--service", "app"]
        )
        assert result.exit_code != 0
        popen.assert_not_called()
        assert "--service deploy" in result.output

    def test_a_serving_fraise_still_tails_its_app_unit(self, tmp_path):
        result, popen = self._invoke(
            tmp_path, ["logs", "api", "production", "--service", "app"]
        )
        assert result.exit_code == 0, result.output
        assert "proj_api_production.service" in popen.call_args[0][0]

    def test_deploy_logs_of_a_non_serving_fraise_are_unchanged(self, tmp_path):
        result, popen = self._invoke(tmp_path, ["logs", "stats", "production"])
        assert result.exit_code == 0, result.output
        assert "fraisier-stats-production@*.service" in popen.call_args[0][0]

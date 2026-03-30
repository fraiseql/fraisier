"""Server setup tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from fraisier.config import FraisierConfig
from fraisier.setup import ServerSetup, SetupAction

SERVER_AWARE_CONFIG = """\
name: tp
fraises:
  my_api:
    type: api
    description: Test API
    environments:
      development:
        app_path: /var/www/my-api-dev
        systemd_service: my-api-dev.service
        git_repo: /var/git/my-api-dev.git
        health_check:
          url: http://localhost:8000/health
          timeout: 10
      staging:
        app_path: /var/www/my-api-stg
        systemd_service: my-api-stg.service
        git_repo: /var/git/my-api-stg.git
        health_check:
          url: http://localhost:8001/health
          timeout: 10
      production:
        app_path: /var/www/my-api
        systemd_service: my-api.service
        git_repo: /var/git/my-api.git
        health_check:
          url: http://localhost:8000/health
          timeout: 30

environments:
  development:
    server: dev.example.io
  staging:
    server: dev.example.io
  production:
    server: prod.example.io
"""


class FakeRunner:
    """Records commands without executing them."""

    def __init__(self, *, failing: set[tuple[str, ...]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._failing = failing or set()

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 300,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if tuple(cmd) in self._failing:
            if check:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _make_config(tmp_path, yaml_content: str) -> FraisierConfig:
    p = tmp_path / "fraises.yaml"
    p.write_text(yaml_content)
    return FraisierConfig(str(p))


MINIMAL_CONFIG = """\
name: tp
fraises:
  my_api:
    type: api
    description: Test API
    environments:
      development:
        app_path: /var/www/my-api-dev
        systemd_service: my-api-dev.service
        git_repo: /var/git/my-api-dev.git
        health_check:
          url: http://localhost:8000/health
          timeout: 10
      production:
        app_path: /var/www/my-api
        systemd_service: my-api.service
        git_repo: /var/git/my-api.git
        health_check:
          url: http://localhost:8000/health
          timeout: 30
"""

MULTI_FRAISE_CONFIG = """\
name: tp
fraises:
  api:
    type: api
    description: API
    environments:
      production:
        app_path: /var/www/api
        systemd_service: api.service
        git_repo: /var/git/api.git
  worker:
    type: api
    description: Worker
    environments:
      production:
        app_path: /var/www/worker
        systemd_service: worker.service
"""

NGINX_CONFIG = """\
name: tp
fraises:
  my_api:
    type: api
    description: Test API
    environments:
      production:
        app_path: /var/www/my-api
        systemd_service: my-api.service
        health_check:
          url: http://localhost:8000/health
          timeout: 30
        nginx:
          server_name: api.example.com
"""


class TestSetupAction:
    def test_fields(self):
        action = SetupAction(
            description="Create dir",
            command=["sudo", "mkdir", "-p", "/var/lib/fraisier"],
            category="directory",
            check=["test", "-d", "/var/lib/fraisier"],
        )
        assert action.description == "Create dir"
        assert action.command == ["sudo", "mkdir", "-p", "/var/lib/fraisier"]
        assert action.category == "directory"
        assert action.check == ["test", "-d", "/var/lib/fraisier"]

    def test_check_defaults_to_none(self):
        action = SetupAction(description="test", command=["echo"], category="test")
        assert action.check is None


class TestPlanDirectories:
    def test_creates_standard_directories(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_directories()

        descriptions = [a.description for a in actions]
        assert any("/var/lib/fraisier" in d for d in descriptions)
        assert any("/var/lib/fraisier/repos" in d for d in descriptions)
        assert any("/run/fraisier" in d for d in descriptions)
        assert any("/etc/fraisier" in d for d in descriptions)

    def test_directory_actions_have_category(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_directories()
        assert all(a.category == "directory" for a in actions)

    def test_mkdir_actions_have_idempotency_check(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_directories()
        mkdir_actions = [a for a in actions if "Create" in a.description]
        assert all(a.check is not None for a in mkdir_actions)

    def test_ownership_set_for_deploy_user_dirs(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_directories()
        chown_actions = [a for a in actions if "ownership" in a.description]
        assert len(chown_actions) == 4
        for a in chown_actions:
            assert "fraisier:fraisier" in " ".join(a.command)


class TestPlanSymlinks:
    def test_creates_symlinks_from_git_repo(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_symlinks()

        assert len(actions) == 2
        assert all(a.category == "symlink" for a in actions)
        assert "/var/git/my-api-dev.git" in " ".join(actions[0].command)
        assert "/var/lib/fraisier/repos/tp_my_api_development.git" in " ".join(
            actions[0].command
        )

    def test_skips_when_no_git_repo(self, tmp_path):
        config = _make_config(
            tmp_path,
            """\
name: tp
fraises:
  my_api:
    type: api
    description: Test
    environments:
      production:
        app_path: /var/www/my-api
        systemd_service: my-api.service
""",
        )
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_symlinks()
        assert actions == []

    def test_environment_filter(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner(), environment="production")
        actions = setup._plan_symlinks()

        assert len(actions) == 1
        assert "tp_my_api_production" in " ".join(actions[0].command)


class TestPlanAppServices:
    def test_produces_copy_actions(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_app_services()

        assert len(actions) == 2
        assert all(a.category == "systemd" for a in actions)
        assert "tp_my_api_development.service" in actions[0].description
        assert "tp_my_api_production.service" in actions[1].description

    def test_environment_filter(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner(), environment="development")
        actions = setup._plan_app_services()

        assert len(actions) == 1
        assert "development" in actions[0].description


class TestPlanWebhookService:
    def test_produces_copy_action(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_webhook_service()

        assert len(actions) == 1
        assert actions[0].category == "systemd"
        assert "fraisier-webhook" in actions[0].description
        assert "/etc/systemd/system/fraisier-webhook.service" in " ".join(
            actions[0].command
        )


class TestPlanEnvFiles:
    def test_produces_install_action(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_env_files()

        assert len(actions) == 1
        assert actions[0].category == "env"
        assert "/etc/fraisier/webhook.env" in " ".join(actions[0].command)

    def test_has_idempotency_check(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_env_files()
        assert actions[0].check is not None


class TestPlanNginx:
    def test_always_includes_gateway(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_nginx()

        gw_actions = [a for a in actions if "gateway" in a.description]
        assert len(gw_actions) == 2

    def test_per_env_nginx_when_configured(self, tmp_path):
        config = _make_config(tmp_path, NGINX_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_nginx()

        env_actions = [a for a in actions if "tp_my_api_production" in a.description]
        assert len(env_actions) == 2

    def test_no_per_env_nginx_when_unconfigured(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_nginx()

        env_actions = [a for a in actions if "gateway" not in a.description]
        assert env_actions == []


class TestPlanSystemdReload:
    def test_includes_daemon_reload(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_systemd_reload()

        assert any("daemon-reload" in " ".join(a.command) for a in actions)

    def test_enables_webhook_and_app_services(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_systemd_reload()

        enable_actions = [a for a in actions if "Enable" in a.description]
        assert len(enable_actions) == 3


class TestPlanValidation:
    def test_includes_nginx_test(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_validation()

        assert any("nginx" in " ".join(a.command) for a in actions)
        assert all(a.category == "validate" for a in actions)

    def test_checks_git_repo_existence(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_validation()

        repo_checks = [a for a in actions if "bare repo" in a.description]
        assert len(repo_checks) == 2


class TestFullPlan:
    def test_plan_returns_all_categories(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup.plan()

        categories = {a.category for a in actions}
        assert "user" in categories
        assert "directory" in categories
        assert "sudoers" in categories
        assert "symlink" in categories
        assert "systemd" in categories
        assert "env" in categories
        assert "nginx" in categories
        assert "validate" in categories

    def test_plan_environment_filter_reduces_actions(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        all_setup = ServerSetup(config, FakeRunner())
        filtered_setup = ServerSetup(config, FakeRunner(), environment="production")

        all_actions = all_setup.plan()
        filtered_actions = filtered_setup.plan()
        assert len(filtered_actions) < len(all_actions)

    def test_project_name_from_config(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        assert setup._infer_project_name() == "tp"

    def test_multi_fraise_project_name(self, tmp_path):
        config = _make_config(tmp_path, MULTI_FRAISE_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        assert setup._infer_project_name() == "tp"


class TestExecute:
    def test_runs_all_commands(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        runner = FakeRunner()
        setup = ServerSetup(config, runner)

        with patch.object(setup._renderer, "render"):
            setup._write_env_file = lambda: None
            results = setup.execute()

        assert len(results) > 0
        assert all(ok for _, ok in results)
        assert len(runner.calls) > 0

    def test_skips_when_check_passes(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        runner = FakeRunner()
        setup = ServerSetup(config, runner)

        with patch.object(setup._renderer, "render"):
            setup._write_env_file = lambda: None
            setup.execute()

        mkdir_cmds = [c for c in runner.calls if "mkdir" in c]
        assert mkdir_cmds == []

    def test_reports_failures(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        runner = FakeRunner(failing={("sudo", "nginx", "-t")})
        setup = ServerSetup(config, runner)

        with patch.object(setup._renderer, "render"):
            setup._write_env_file = lambda: None
            results = setup.execute()

        failed = [(a, ok) for a, ok in results if not ok]
        assert len(failed) >= 1
        assert any("nginx" in a.description for a, _ in failed)


class TestEnvFile:
    def test_writes_env_file(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        setup._write_env_file()

        output = Path(config.scaffold.output_dir) / "fraisier-webhook.env"
        assert output.exists()
        content = output.read_text()
        assert "FRAISIER_WEBHOOK_SECRET=" in content
        assert "FRAISIER_CONFIG=" in content
        assert "FRAISIER_PORT=8080" in content


class TestWebhookTemplate:
    def test_scaffold_renders_webhook_service(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        files = renderer.render(dry_run=True)
        assert "fraisier-webhook.service" in files

    def test_webhook_template_contains_readwrite_paths(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        output = Path(config.scaffold.output_dir) / "fraisier-webhook.service"
        assert output.exists()
        content = output.read_text()
        assert "ReadWritePaths=/var/www/my-api-dev" in content
        assert "ReadWritePaths=/var/www/my-api" in content
        assert "fraisier-webhook" in content


class TestCLI:
    def test_dry_run_exits_cleanly(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        runner = CliRunner()

        with patch("fraisier.config.get_config", return_value=config):
            from fraisier.cli.main import main

            result = runner.invoke(main, ["setup", "--dry-run"])

        assert result.exit_code == 0
        assert "actions would be executed" in result.output

    def test_interactive_aborts_on_no(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        runner = CliRunner()

        with patch("fraisier.config.get_config", return_value=config):
            from fraisier.cli.main import main

            result = runner.invoke(main, ["setup"], input="n\n")

        assert result.exit_code == 0
        assert "Aborted" in result.output

    def test_environment_flag(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        runner = CliRunner()

        with patch("fraisier.config.get_config", return_value=config):
            from fraisier.cli.main import main

            result = runner.invoke(
                main, ["setup", "--dry-run", "--environment", "production"]
            )

        assert result.exit_code == 0
        assert "actions would be executed" in result.output

    def test_server_flag(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        runner = CliRunner()

        with patch("fraisier.config.get_config", return_value=config):
            from fraisier.cli.main import main

            result = runner.invoke(
                main, ["setup", "--dry-run", "--server", "prod.example.io"]
            )

        assert result.exit_code == 0
        assert "actions would be executed" in result.output

    def test_server_and_environment_mutually_exclusive(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        runner = CliRunner()

        with patch("fraisier.config.get_config", return_value=config):
            from fraisier.cli.main import main

            result = runner.invoke(
                main,
                [
                    "setup",
                    "--dry-run",
                    "--server",
                    "prod.example.io",
                    "--environment",
                    "production",
                ],
            )

        assert result.exit_code != 0
        assert "mutually exclusive" in result.output


class TestServerFiltering:
    def test_server_flag_filters_environments(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        setup = ServerSetup(config, FakeRunner(), server="prod.example.io")
        actions = setup._plan_app_services()

        assert len(actions) == 1
        assert "production" in actions[0].description

    def test_server_flag_matches_multiple_environments(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        setup = ServerSetup(config, FakeRunner(), server="dev.example.io")
        actions = setup._plan_app_services()

        descriptions = [a.description for a in actions]
        assert len(actions) == 2
        assert any("development" in d for d in descriptions)
        assert any("staging" in d for d in descriptions)

    def test_unknown_server_provisions_all(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        setup = ServerSetup(config, FakeRunner(), server="unknown.host")
        actions = setup._plan_app_services()

        assert len(actions) == 3

    def test_auto_detect_hostname(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        setup = ServerSetup(config, FakeRunner())

        with (
            patch("fraisier.setup.socket.getfqdn", return_value="prod.example.io"),
            patch("fraisier.setup.socket.gethostname", return_value="prod"),
        ):
            actions = setup._plan_app_services()

        assert len(actions) == 1
        assert "production" in actions[0].description

    def test_auto_detect_falls_back_to_short_hostname(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        # Use short hostname as the server value in config
        server_config = SERVER_AWARE_CONFIG.replace(
            "server: prod.example.io", "server: prod"
        )
        config = _make_config(tmp_path, server_config)
        setup = ServerSetup(config, FakeRunner())

        with (
            patch("fraisier.setup.socket.getfqdn", return_value="prod.example.io"),
            patch("fraisier.setup.socket.gethostname", return_value="prod"),
        ):
            actions = setup._plan_app_services()

        assert len(actions) == 1
        assert "production" in actions[0].description

    def test_auto_detect_no_match_provisions_all(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        setup = ServerSetup(config, FakeRunner())

        with (
            patch("fraisier.setup.socket.getfqdn", return_value="other.host"),
            patch("fraisier.setup.socket.gethostname", return_value="other"),
        ):
            actions = setup._plan_app_services()

        assert len(actions) == 3

    def test_no_global_environments_provisions_all(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())

        with (
            patch("fraisier.setup.socket.getfqdn", return_value="any.host"),
            patch("fraisier.setup.socket.gethostname", return_value="any"),
        ):
            actions = setup._plan_app_services()

        assert len(actions) == 2

    def test_environment_flag_takes_priority_over_auto_detect(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        setup = ServerSetup(config, FakeRunner(), environment="staging")

        with (
            patch("fraisier.setup.socket.getfqdn", return_value="prod.example.io"),
            patch("fraisier.setup.socket.gethostname", return_value="prod"),
        ):
            actions = setup._plan_app_services()

        assert len(actions) == 1
        assert "staging" in actions[0].description

    def test_server_filter_applies_to_full_plan(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        all_setup = ServerSetup(config, FakeRunner(), server="dev.example.io")
        prod_setup = ServerSetup(config, FakeRunner(), server="prod.example.io")

        # Patch hostname auto-detect to not interfere
        with (
            patch("fraisier.setup.socket.getfqdn", return_value="localhost"),
            patch("fraisier.setup.socket.gethostname", return_value="localhost"),
        ):
            dev_actions = all_setup.plan()
            prod_actions = prod_setup.plan()

        assert len(prod_actions) < len(dev_actions)


class TestPlanUsers:
    """Setup creates system accounts for deploy and app users (#28)."""

    def test_creates_deploy_user(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_users()

        assert len(actions) >= 1
        assert all(a.category == "user" for a in actions)
        cmds = [a.command for a in actions]
        assert any("fraisier" in cmd for cmd in cmds)

    def test_creates_per_env_deploy_user(self, tmp_path):
        config = _make_config(
            tmp_path,
            """
name: tp
scaffold:
  deploy_user: fraisier
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        deploy_user: prod-deployer
""",
        )
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_users()

        user_names = [a.command[-1] for a in actions]
        assert "fraisier" in user_names
        assert "prod-deployer" in user_names

    def test_creates_app_user_from_service_config(self, tmp_path):
        config = _make_config(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        service:
          user: myapp
""",
        )
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_users()

        user_names = [a.command[-1] for a in actions]
        assert "fraisier" in user_names
        assert "myapp" in user_names

    def test_deduplicates_users(self, tmp_path):
        config = _make_config(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      dev:
        app_path: /var/www/dev
        service:
          user: myapp
      staging:
        app_path: /var/www/staging
        service:
          user: myapp
""",
        )
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_users()

        user_names = [a.command[-1] for a in actions]
        assert user_names.count("myapp") == 1

    def test_users_before_directories_in_plan(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup.plan()

        categories = [a.category for a in actions]
        first_user = categories.index("user")
        first_dir = categories.index("directory")
        assert first_user < first_dir

    def test_idempotent_check(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_users()

        for action in actions:
            assert action.check is not None
            assert action.check[0] == "id"


class TestPlanAppPermissions:
    """Setup configures ownership when app_user != deploy_user (#28)."""

    def test_split_user_creates_chown_and_group(self, tmp_path):
        config = _make_config(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        service:
          user: myapp
""",
        )
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_app_permissions()

        categories = [a.category for a in actions]
        assert all(c == "permissions" for c in categories)
        cmds = [" ".join(a.command) for a in actions]
        assert any("chown" in c and "myapp" in c for c in cmds)
        assert any("usermod" in c and "myapp" in c for c in cmds)
        assert any("chmod" in c and "g+rwx" in c for c in cmds)

    def test_single_user_creates_simple_chown(self, tmp_path):
        config = _make_config(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
""",
        )
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_app_permissions()

        assert len(actions) == 1
        assert "chown" in " ".join(actions[0].command)
        assert "fraisier" in " ".join(actions[0].command)

    def test_permissions_category_in_plan(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup.plan()

        categories = {a.category for a in actions}
        assert "permissions" in categories

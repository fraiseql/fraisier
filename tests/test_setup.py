"""Server setup tests."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fraisier.config import FraisierConfig
from fraisier.errors import ValidationError
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
        assert len(chown_actions) == 5
        for a in chown_actions:
            assert "fraisier:fraisier" in " ".join(a.command)

    def test_creates_scaffold_state_dir(self, tmp_path):
        """setup provisions the tree the socket helper reads from (#284).

        Until this directory holds an install.sh the helper daemon exits at
        startup (``scaffold_install_helper.py:228-230``), so the deploy silently
        drops onto the subprocess fallback.
        """
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_directories()

        state_dir = config.scaffold_state_dir
        assert state_dir == "/var/lib/fraisier/tp/scaffold"
        mkdirs = [a for a in actions if a.command[:3] == ["sudo", "mkdir", "-p"]]
        assert any(a.command[3] == state_dir for a in mkdirs)

    def test_scaffold_state_dir_owned_by_deploy_user(self, tmp_path):
        """Deploy-time regeneration runs as deploy_user and must refresh it.

        Same reason ``bootstrap.py:257-273`` chowns it.
        """
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_directories()

        state_dir = config.scaffold_state_dir
        chowns = [a for a in actions if a.command[:2] == ["sudo", "chown"]]
        assert any(
            a.command[2] == "fraisier:fraisier" and a.command[3] == state_dir
            for a in chowns
        )


class TestPlanScaffoldState:
    """`setup` also persists its rendered tree into scaffold_state_dir (#284)."""

    def test_copies_rendered_tree_into_state_dir(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup.plan()

        copies = [
            a for a in actions if a.category == "scaffold" and "cp" in a.command[-1]
        ]
        assert len(copies) == 1
        assert (
            "cp -a scripts/generated/. /var/lib/fraisier/tp/scaffold/"
            in copies[0].command[-1]
        )

    def test_webhook_env_file_is_not_persisted(self, tmp_path):
        """It holds FRAISIER_WEBHOOK_SECRET and state_dir is world-readable.

        Its only install target is /etc/fraisier/{project}.webhook.env at 0640
        (``_plan_env_files``), and the deploy path's renderer never writes it
        into state_dir either.
        """
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup.plan()

        copies = [
            a for a in actions if a.category == "scaffold" and "cp" in a.command[-1]
        ]
        assert (
            "rm -f /var/lib/fraisier/tp/scaffold/fraisier-tp.webhook.env"
            in copies[0].command[-1]
        )

    def test_persisted_tree_is_chowned_to_deploy_user(self, tmp_path):
        """`cp -a` preserves the operator's ownership; deploy_user must own it."""
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup.plan()

        chowns = [
            a
            for a in actions
            if a.category == "scaffold" and a.command[:3] == ["sudo", "chown", "-R"]
        ]
        assert len(chowns) == 1
        assert chowns[0].command[3:] == [
            "fraisier:fraisier",
            "/var/lib/fraisier/tp/scaffold",
        ]

    def test_copy_ordered_after_the_directory_that_receives_it(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup.plan()

        mkdir_idx = next(
            i
            for i, a in enumerate(actions)
            if a.command[:3] == ["sudo", "mkdir", "-p"]
            and a.command[3] == "/var/lib/fraisier/tp/scaffold"
        )
        copy_idx = next(
            i
            for i, a in enumerate(actions)
            if a.category == "scaffold" and "cp" in a.command[-1]
        )
        chown_idx = next(
            i
            for i, a in enumerate(actions)
            if a.category == "scaffold" and a.command[:3] == ["sudo", "chown", "-R"]
        )
        assert mkdir_idx < copy_idx < chown_idx

    def test_install_sources_still_resolve_against_output_dir(self, tmp_path):
        """Option 2, not Option 1 — the review-then-install loop is unchanged.

        `setup` renders into a CWD-relative tree the operator inspects and
        installs what they just read; `state_dir` is added as what the machine
        consumes, not substituted as the install source.
        """
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())

        sources = [
            *setup._plan_sudoers(),
            *setup._plan_app_services(),
            *setup._plan_webhook_service(),
            *setup._plan_env_files(),
            *setup._plan_nginx(),
        ]
        installs = [a for a in sources if a.command[1] in {"cp", "install"}]
        assert len(installs) == 6
        for action in installs:
            src = action.command[-2]
            assert src.startswith("scripts/generated/"), action.description

    def test_persisted_tree_supplies_the_helpers_allowed_script(self, tmp_path):
        """The copied tree carries exactly the path baked into the helper unit.

        The unit's ExecStart argument is ``{state_dir}/install.sh``
        (``renderer.py:936-941``) and the daemon exits 1 at startup when that
        path is absent (``scaffold_install_helper.py:228-230``).  `install.sh`
        renders at the *root* of the tree `setup` copies, so copying
        ``output_dir/.`` into ``state_dir/`` is what closes the gap — this is
        the assertion that makes the two command strings above mean something.
        """
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = _make_config(tmp_path, MINIMAL_CONFIG)
        renderer = ScaffoldRenderer(config)
        renderer.output_dir = tmp_path / "generated"
        renderer.render()

        assert (renderer.output_dir / "install.sh").is_file()
        unit = (
            renderer.output_dir
            / "systemd"
            / "fraisier-tp-scaffold-install-helper.service"
        ).read_text()
        assert "/var/lib/fraisier/tp/scaffold/install.sh" in unit


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

    def test_has_idempotency_check(self, tmp_path):
        """Symlink actions have a check to avoid overwriting existing targets (#35)."""
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_symlinks()

        for action in actions:
            assert action.check is not None
            assert "readlink" in " ".join(action.check)

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
        assert "my-api-dev.service" in actions[0].description
        assert "my-api.service" in actions[1].description

    def test_uses_systemd_service_from_config(self, tmp_path):
        """systemd_service from env config is used as the installed filename (#35)."""
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_app_services()

        # The destination should use systemd_service from config
        dev_dst = actions[0].command[-1]
        assert dev_dst == "/etc/systemd/system/my-api-dev.service"
        prod_dst = actions[1].command[-1]
        assert prod_dst == "/etc/systemd/system/my-api.service"

        # The source should still use the generated name
        dev_src = actions[0].command[-2]
        assert "tp_my_api_development.service" in dev_src

    def test_falls_back_to_generated_name(self, tmp_path):
        """Without systemd_service, the generated name is used."""
        no_svc_config = """\
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/my-api
        git_repo: /var/git/my-api.git
"""
        config = _make_config(tmp_path, no_svc_config)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_app_services()

        assert len(actions) == 1
        assert "tp_my_api_production.service" in actions[0].description
        dst = "/etc/systemd/system/tp_my_api_production.service"
        assert actions[0].command[-1] == dst

    def test_environment_filter(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner(), environment="development")
        actions = setup._plan_app_services()

        assert len(actions) == 1
        assert "my-api-dev.service" in actions[0].description


class TestPlanWebhookService:
    def test_produces_copy_action(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_webhook_service()

        assert len(actions) == 1
        assert actions[0].category == "systemd"
        assert "fraisier-tp-webhook" in actions[0].description
        assert "/etc/systemd/system/fraisier-tp-webhook.service" in " ".join(
            actions[0].command
        )


class TestPlanEnvFiles:
    def test_produces_install_action(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_env_files()

        assert len(actions) == 1
        assert actions[0].category == "env"
        assert "/etc/fraisier/tp.webhook.env" in " ".join(actions[0].command)

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

    def test_enable_uses_systemd_service_from_config(self, tmp_path):
        """Enable commands use systemd_service from env config (#35)."""
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_systemd_reload()

        app_enables = [
            a
            for a in actions
            if "Enable" in a.description and "webhook" not in a.description
        ]
        assert len(app_enables) == 2
        assert "my-api-dev.service" in app_enables[0].description
        assert "my-api.service" in app_enables[1].description


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
            setup._write_env_file = lambda: None  # ty: ignore[invalid-assignment]
            results = setup.execute()

        assert len(results) > 0
        assert all(ok for _, ok in results)
        assert len(runner.calls) > 0

    def test_skips_when_check_passes(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        runner = FakeRunner()
        setup = ServerSetup(config, runner)

        with patch.object(setup._renderer, "render"):
            setup._write_env_file = lambda: None  # ty: ignore[invalid-assignment]
            setup.execute()

        mkdir_cmds = [c for c in runner.calls if "mkdir" in c]
        assert mkdir_cmds == []

    def test_reports_failures(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        runner = FakeRunner(failing={("sudo", "nginx", "-t")})
        setup = ServerSetup(config, runner)

        with patch.object(setup._renderer, "render"):
            setup._write_env_file = lambda: None  # ty: ignore[invalid-assignment]
            results = setup.execute()

        failed = [(a, ok) for a, ok in results if not ok]
        assert len(failed) >= 1
        assert any("nginx" in a.description for a, _ in failed)


class TestEnvFile:
    def test_writes_env_file(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())
        setup._write_env_file()

        output = Path(config.scaffold.output_dir) / "fraisier-tp.webhook.env"
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
        assert "fraisier-tp-webhook.service" in files

    def test_webhook_template_contains_readwrite_paths(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        output = Path(config.scaffold.output_dir) / "fraisier-tp-webhook.service"
        assert output.exists()
        content = output.read_text()
        assert "ReadWritePaths=/var/www/my-api-dev" in content
        assert "ReadWritePaths=/var/www/my-api" in content
        assert "fraisier-webhook" in content


class TestCLI:
    def test_dry_run_exits_cleanly(self, tmp_path):
        # `-c`, not the get_config patch: fraisier/cli/main.py binds
        # get_config with a `from` import, so patching fraisier.config leaves
        # the CLI reading the repository's own fraises.yaml — a config this
        # machine is not a declared host of, which #331 now refuses.
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        runner = CliRunner()

        from fraisier.cli.main import main

        result = runner.invoke(
            main, ["-c", str(config.config_path), "setup", "--dry-run"]
        )

        assert result.exit_code == 0
        assert "actions would be executed" in result.output

    def test_interactive_aborts_on_no(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        runner = CliRunner()

        from fraisier.cli.main import main

        result = runner.invoke(
            main, ["-c", str(config.config_path), "setup"], input="n\n"
        )

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
        # `-c` rather than the get_config patch: the CLI group builds
        # ctx.obj["config"] from its own load, so without it this ran against
        # the repository's own fraises.yaml and --server named a server that
        # config never declares. Harmless while an unknown --server rendered a
        # silently-empty unit; a hard error since #325.
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        runner = CliRunner()

        with patch("fraisier.config.get_config", return_value=config):
            from fraisier.cli.main import main

            result = runner.invoke(
                main,
                [
                    "-c",
                    str(config.config_path),
                    "setup",
                    "--dry-run",
                    "--server",
                    "prod.example.io",
                ],
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
        assert "my-api.service" in actions[0].description

    def test_server_flag_matches_multiple_environments(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        setup = ServerSetup(config, FakeRunner(), server="dev.example.io")
        actions = setup._plan_app_services()

        descriptions = [a.description for a in actions]
        assert len(actions) == 2
        assert any("my-api-dev" in d for d in descriptions)
        assert any("my-api-stg" in d for d in descriptions)

    def test_unknown_server_is_refused(self, tmp_path):
        """#331: an unmatched --server used to widen to every environment."""
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        setup = ServerSetup(config, FakeRunner(), server="unknown.host")

        with pytest.raises(ValidationError, match=re.escape("unknown.host")):
            setup._plan_app_services()

    def test_auto_detect_hostname(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        setup = ServerSetup(config, FakeRunner())

        with (
            patch(
                "fraisier.scaffold.renderer.local_hostnames",
                return_value=["prod", "prod.example.io"],
            ),
        ):
            actions = setup._plan_app_services()

        assert len(actions) == 1
        assert "my-api.service" in actions[0].description

    def test_auto_detect_falls_back_to_short_hostname(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        # Use short hostname as the server value in config
        server_config = SERVER_AWARE_CONFIG.replace(
            "server: prod.example.io", "server: prod"
        )
        config = _make_config(tmp_path, server_config)
        setup = ServerSetup(config, FakeRunner())

        with (
            patch(
                "fraisier.scaffold.renderer.local_hostnames",
                return_value=["prod", "prod.example.io"],
            ),
        ):
            actions = setup._plan_app_services()

        assert len(actions) == 1
        assert "my-api.service" in actions[0].description

    def test_auto_detect_no_match_is_refused(self, tmp_path):
        """#331: 'cannot tell which host I am' must not mean 'provision all'."""
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        setup = ServerSetup(config, FakeRunner())

        with (
            patch(
                "fraisier.scaffold.renderer.local_hostnames",
                return_value=["other", "other.host"],
            ),
            pytest.raises(ValidationError, match="matches no host"),
        ):
            setup._plan_app_services()

    def test_no_global_environments_provisions_all(self, tmp_path):
        config = _make_config(tmp_path, MINIMAL_CONFIG)
        setup = ServerSetup(config, FakeRunner())

        with (
            patch(
                "fraisier.scaffold.renderer.local_hostnames",
                return_value=["any", "any.host"],
            ),
        ):
            actions = setup._plan_app_services()

        assert len(actions) == 2

    def test_environment_flag_takes_priority_over_auto_detect(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        setup = ServerSetup(config, FakeRunner(), environment="staging")

        with (
            patch(
                "fraisier.scaffold.renderer.local_hostnames",
                return_value=["prod", "prod.example.io"],
            ),
        ):
            actions = setup._plan_app_services()

        assert len(actions) == 1
        assert "my-api-stg.service" in actions[0].description

    def test_server_filter_applies_to_full_plan(self, tmp_path):
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        all_setup = ServerSetup(config, FakeRunner(), server="dev.example.io")
        prod_setup = ServerSetup(config, FakeRunner(), server="prod.example.io")

        # Pin host auto-detect so it cannot interfere with the --server filter
        with (
            patch(
                "fraisier.scaffold.renderer.local_hostnames",
                return_value=["localhost"],
            ),
        ):
            dev_actions = all_setup.plan()
            prod_actions = prod_setup.plan()

        assert len(prod_actions) < len(dev_actions)

    def test_per_fraise_server_field_used_for_filtering(self, tmp_path):
        """Server field in per-fraise environments is used for filtering (#35)."""
        per_fraise_server_config = """\
name: tp
fraises:
  my_api:
    type: api
    environments:
      development:
        app_path: /var/www/my-api-dev
        server: dev.example.io
        git_repo: /var/git/my-api-dev.git
      production:
        app_path: /var/www/my-api
        server: prod.example.io
        git_repo: /var/git/my-api.git
"""
        config = _make_config(tmp_path, per_fraise_server_config)
        setup = ServerSetup(config, FakeRunner(), server="dev.example.io")
        actions = setup._plan_app_services()

        assert len(actions) == 1
        assert "development" in actions[0].description

    def test_auto_detect_no_match_names_the_alternatives(self, tmp_path):
        """The #35 warning became the #331 error, and carries the way out.

        The warning this replaces fired for years and was acted on by nobody,
        which is why #331 exists at all; a louder warning would have been a
        fourth instance of the same non-response.
        """
        config = _make_config(tmp_path, SERVER_AWARE_CONFIG)
        setup = ServerSetup(config, FakeRunner())

        with (
            patch(
                "fraisier.scaffold.renderer.local_hostnames",
                return_value=["other", "other.host"],
            ),
            pytest.raises(ValidationError) as exc,
        ):
            setup._plan_app_services()

        message = str(exc.value)
        assert "dev.example.io" in message
        assert "prod.example.io" in message
        assert "--all-environments" in message


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


class TestPlanGitSafeDirectory:
    """Setup configures git safe.directory for deploy user (#31)."""

    def test_adds_safe_directory_when_users_differ(self, tmp_path):
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
        git_repo: /var/git/api.git
        service:
          user: myapp
""",
        )
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_git_safe_directory()

        assert len(actions) == 2
        assert all(a.category == "git" for a in actions)
        cmds = [" ".join(a.command) for a in actions]
        assert any("/var/git/api.git" in c for c in cmds)
        assert any("/var/www/api" in c for c in cmds)
        for cmd in cmds:
            assert "sudo -u fraisier" in cmd or ("sudo" in cmd and "fraisier" in cmd)

    def test_no_actions_when_users_match(self, tmp_path):
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
        git_repo: /var/git/api.git
""",
        )
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_git_safe_directory()

        assert actions == []

    def test_no_actions_when_no_git_repo(self, tmp_path):
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
        actions = setup._plan_git_safe_directory()

        assert actions == []

    def test_only_adds_git_repo_when_no_app_path(self, tmp_path):
        config = _make_config(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        git_repo: /var/git/api.git
        service:
          user: myapp
""",
        )
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_git_safe_directory()

        assert len(actions) == 1
        assert "/var/git/api.git" in " ".join(actions[0].command)

    def test_deduplicates_paths(self, tmp_path):
        config = _make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      dev:
        app_path: /var/www/api
        git_repo: /var/git/api.git
        service:
          user: myapp
      staging:
        app_path: /var/www/api
        git_repo: /var/git/api.git
        service:
          user: myapp
""",
        )
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_git_safe_directory()

        assert len(actions) == 2

    def test_uses_per_env_deploy_user(self, tmp_path):
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
        git_repo: /var/git/api.git
        deploy_user: prod-deployer
        service:
          user: myapp
""",
        )
        setup = ServerSetup(config, FakeRunner())
        actions = setup._plan_git_safe_directory()

        cmds = [" ".join(a.command) for a in actions]
        assert all("prod-deployer" in c for c in cmds)

    def test_git_category_in_plan(self, tmp_path):
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
        git_repo: /var/git/api.git
        service:
          user: myapp
""",
        )
        setup = ServerSetup(config, FakeRunner())
        actions = setup.plan()

        categories = [a.category for a in actions]
        assert "git" in categories
        # git safe.directory should come after users but before symlinks
        first_git = categories.index("git")
        first_symlink = categories.index("symlink")
        assert first_git < first_symlink

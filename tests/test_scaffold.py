"""Infrastructure scaffold tests."""

from fraisier.config import FraisierConfig


class TestScaffoldConfigParsing:
    """scaffold: section must parse from fraises.yaml with defaults."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_scaffold_section_parses(self, tmp_path):
        """Full scaffold section parses correctly."""
        config = self._make_config(
            tmp_path,
            """
fraises: {}
scaffold:
  output_dir: scripts/generated
  deploy_user: my_project_app
  systemd:
    security_hardening: true
    memory_max_default: "4G"
  nginx:
    ssl_provider: letsencrypt
    cors_origins: ["*.example.io", "localhost:*"]
    rate_limit: "10r/s"
    restricted_paths: ["/utilities/", "/admin/"]
  github_actions:
    python_versions: ["3.12"]
    test_command: "uv run pytest"
    lint_command: "uv run ruff check"
    format_command: "uv run ruff format --check"
""",
        )
        sc = config.scaffold
        assert sc.output_dir == "scripts/generated"
        assert sc.deploy_user == "my_project_app"
        assert sc.systemd.security_hardening is True
        assert sc.systemd.memory_max_default == "4G"
        assert sc.nginx.ssl_provider == "letsencrypt"
        assert "*.example.io" in sc.nginx.cors_origins
        assert sc.nginx.rate_limit == "10r/s"
        assert "/admin/" in sc.nginx.restricted_paths
        assert "3.12" in sc.github_actions.python_versions
        assert sc.github_actions.test_command == "uv run pytest"
        assert sc.github_actions.format_command == "uv run ruff format --check"

    def test_scaffold_section_defaults(self, tmp_path):
        """Missing scaffold section uses sensible defaults."""
        config = self._make_config(tmp_path, "fraises: {}\n")
        sc = config.scaffold
        assert sc.output_dir == "scripts/generated"
        assert sc.deploy_user == "fraisier"
        assert sc.systemd.security_hardening is True
        assert sc.systemd.memory_max_default == "4G"
        assert sc.nginx.ssl_provider == "letsencrypt"
        assert sc.github_actions.python_versions == ["3.12"]

    def test_per_fraise_scaffold_fields(self, tmp_path):
        """Per-fraise fields: schema_command, compile_command, etc."""
        config = self._make_config(
            tmp_path,
            """
fraises:
  management:
    type: api
    schema_command: make schema-export
    compile_command: make schema-compile
    gateway_toml: federation/gateway.toml
    external_db: false
    environments:
      development:
        worker_count: 1
        memory_max: "2G"
      production:
        worker_count: 4
        memory_max: "8G"
""",
        )
        fraise = config.get_fraise("management")
        assert fraise["schema_command"] == "make schema-export"
        assert fraise["compile_command"] == "make schema-compile"
        assert fraise["gateway_toml"] == "federation/gateway.toml"
        assert fraise["external_db"] is False

        dev = config.get_fraise_environment("management", "development")
        assert dev["worker_count"] == 1
        assert dev["memory_max"] == "2G"

        prod = config.get_fraise_environment("management", "production")
        assert prod["worker_count"] == 4
        assert prod["memory_max"] == "8G"

    def test_scaffold_deploy_user_inherits_from_deployment(self, tmp_path):
        """scaffold.deploy_user falls back to deployment.deploy_user."""
        config = self._make_config(
            tmp_path,
            """
fraises: {}
deployment:
  deploy_user: deploy_bot
""",
        )
        sc = config.scaffold
        assert sc.deploy_user == "deploy_bot"


class TestScaffoldRenderer:
    """Renderer runs core templates, then provider templates."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_renderer_writes_core_templates(self, tmp_path):
        """Core templates are rendered to output_dir."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 2
        memory_max: "4G"
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        renderer = ScaffoldRenderer(config)
        files = renderer.render()
        assert len(files) > 0
        # At least some files should exist in output dir
        output_dir = tmp_path / "output"
        assert output_dir.exists()

    def test_renderer_dry_run_does_not_write(self, tmp_path):
        """Dry-run returns file list without writing."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 2
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        renderer = ScaffoldRenderer(config)
        files = renderer.render(dry_run=True)
        assert len(files) > 0
        output_dir = tmp_path / "output"
        assert not output_dir.exists()

    def test_renderer_no_overlap_core_and_provider(self, tmp_path):
        """Core and provider output paths don't overlap."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 2
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        renderer = ScaffoldRenderer(config)
        core_files = renderer.get_core_template_paths()
        provider_files = renderer.get_provider_template_paths()
        overlap = set(core_files) & set(provider_files)
        assert overlap == set()


_REQUIRED_SECURITY_DIRECTIVES = [
    "NoNewPrivileges=true",
    "ProtectSystem=strict",
    "ProtectHome=true",
    "PrivateTmp=true",
    "PrivateDevices=true",
    "ProtectKernelTunables=true",
    "ProtectKernelModules=true",
    "ProtectControlGroups=true",
    "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
    "SystemCallFilter=~@clock @debug @module @mount @obsolete @reboot @swap",
]


class TestSystemdServiceTemplates:
    """Systemd service templates with full security hardening."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_service_unit_has_all_security_directives(self, tmp_path):
        """Rendered .service has ALL required security directives."""
        config = self._make_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 4
        memory_max: "8G"
scaffold:
  output_dir: {output}
  deploy_user: my_app
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        svc_path = tmp_path / "output" / "systemd" / "my_api_production.service"
        assert svc_path.exists(), f"Expected {svc_path} to exist"
        content = svc_path.read_text()

        for directive in _REQUIRED_SECURITY_DIRECTIVES:
            assert directive in content, f"Missing security directive: {directive}"

    def test_service_unit_has_correct_exec_start(self, tmp_path):
        """Rendered .service has correct ExecStart with worker count."""
        config = self._make_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 4
        memory_max: "8G"
scaffold:
  output_dir: {output}
  deploy_user: my_app
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        svc_path = tmp_path / "output" / "systemd" / "my_api_production.service"
        content = svc_path.read_text()

        assert "ExecStart=" in content
        assert "User=my_app" in content
        assert "MemoryMax=8G" in content

    def test_service_memory_max_uses_default(self, tmp_path):
        """MemoryMax uses scaffold default when not set per-env."""
        config = self._make_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      development:
        worker_count: 1
scaffold:
  output_dir: {output}
  systemd:
    memory_max_default: "2G"
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        svc_path = tmp_path / "output" / "systemd" / "my_api_development.service"
        content = svc_path.read_text()
        assert "MemoryMax=2G" in content


class TestSystemdTimerTemplates:
    """Timer templates for deploy checker and backup."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_deploy_checker_timer_rendered(self, tmp_path):
        """deploy-checker.timer is generated with poll interval."""
        config = self._make_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 2
deployment:
  poll_interval_seconds: 120
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        timer_path = tmp_path / "output" / "systemd" / "deploy-checker.timer"
        assert timer_path.exists()
        content = timer_path.read_text()
        assert "[Timer]" in content
        assert "OnUnitActiveSec=" in content

    def test_backup_timer_rendered(self, tmp_path):
        """backup.timer is generated."""
        config = self._make_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 2
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        timer_path = tmp_path / "output" / "systemd" / "backup.timer"
        assert timer_path.exists()
        content = timer_path.read_text()
        assert "[Timer]" in content


_POLL_DEPLOY_SECURITY_DIRECTIVES = [
    "NoNewPrivileges=true",
    "ProtectSystem=strict",
    "ProtectHome=true",
    "PrivateTmp=true",
    "PrivateDevices=true",
    "ProtectKernelTunables=true",
    "ProtectKernelModules=true",
    "ProtectControlGroups=true",
    "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
]


class TestSystemdServiceHardening:
    """All scaffolded systemd services have security hardening."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_poll_deploy_service_has_security_directives(self, tmp_path):
        """poll-deploy.service has all security hardening directives."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 2
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        renderer = ScaffoldRenderer(config)
        renderer.render()

        svc_path = tmp_path / "output" / "poll-deploy.service"
        assert svc_path.exists()
        content = svc_path.read_text()

        for directive in _POLL_DEPLOY_SECURITY_DIRECTIVES:
            assert directive in content, f"Missing directive: {directive}"
        assert "ReadWritePaths=" in content

    def test_backup_service_has_security_directives(self, tmp_path):
        """backup.service has all security hardening directives."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 2
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        renderer = ScaffoldRenderer(config)
        renderer.render()

        svc_path = tmp_path / "output" / "systemd" / "backup.service"
        assert svc_path.exists()
        content = svc_path.read_text()

        for directive in _POLL_DEPLOY_SECURITY_DIRECTIVES:
            assert directive in content, f"Missing directive: {directive}"
        assert "ReadWritePaths=/var/backups/" in content


class TestNginxTemplate:
    """Nginx reverse proxy template with SSL, CORS, security headers."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_nginx_config_has_upstream_and_cors(self, tmp_path):
        """Rendered nginx config has upstream, CORS, security headers."""
        config = self._make_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 2
scaffold:
  output_dir: {output}
  nginx:
    ssl_provider: letsencrypt
    cors_origins: ["*.example.io"]
    rate_limit: "10r/s"
    restricted_paths: ["/admin/"]
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        nginx_path = tmp_path / "output" / "nginx" / "gateway.conf"
        assert nginx_path.exists()
        content = nginx_path.read_text()
        assert "upstream" in content
        assert "proxy_pass" in content
        assert "Access-Control-Allow-Origin" in content
        assert "X-Frame-Options" in content
        assert "/admin/" in content


_SCAFFOLD_YAML = """
fraises:
  my_api:
    type: api
    schema_command: make schema-export
    compile_command: make schema-compile
    external_db: false
    environments:
      production:
        worker_count: 4
        memory_max: "8G"
      development:
        worker_count: 1
scaffold:
  output_dir: {output}
  deploy_user: my_app
  nginx:
    cors_origins: ["*.example.io"]
    restricted_paths: ["/admin/"]
  github_actions:
    python_versions: ["3.12"]
    test_command: "uv run pytest"
deployment:
  strategies:
    development: rebuild
    production: migrate
"""


def _make_full_config(tmp_path):
    p = tmp_path / "fraises.yaml"
    p.write_text(_SCAFFOLD_YAML.format(output=str(tmp_path / "output")))
    return FraisierConfig(p)


class TestGithubActionsTemplates:
    """GitHub Actions workflow templates."""

    def test_deploy_yml_rendered(self, tmp_path):
        """deploy.yml is generated with correct structure."""
        config = _make_full_config(tmp_path)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        deploy_path = tmp_path / "output" / "deploy.yml"
        assert deploy_path.exists()
        content = deploy_path.read_text()
        assert "name:" in content
        assert "jobs:" in content or "steps:" in content


class TestSudoersAndInstall:
    """Sudoers fragment and install script."""

    def test_sudoers_rendered(self, tmp_path):
        """sudoers grants deploy_user correct permissions."""
        config = _make_full_config(tmp_path)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        sudoers_path = tmp_path / "output" / "sudoers"
        assert sudoers_path.exists()
        content = sudoers_path.read_text()
        assert "my_app" in content
        assert "systemctl" in content

    def test_install_sh_rendered(self, tmp_path):
        """install.sh is generated and idempotent-friendly."""
        config = _make_full_config(tmp_path)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        install_path = tmp_path / "output" / "install.sh"
        assert install_path.exists()
        content = install_path.read_text()
        assert "#!/" in content
        assert "my_app" in content


class TestConfitureTemplates:
    """confiture config templates."""

    def test_confiture_yaml_rendered(self, tmp_path):
        """confiture.yaml is generated for non-external_db fraises."""
        config = _make_full_config(tmp_path)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        confiture_path = tmp_path / "output" / "confiture.yaml"
        assert confiture_path.exists()
        content = confiture_path.read_text()
        assert "my_api" in content


class TestShellScriptTemplates:
    """backup.sh, db_reset.sh, db_deploy.sh."""

    def test_backup_sh_rendered(self, tmp_path):
        """backup.sh is generated with pg_dump."""
        config = _make_full_config(tmp_path)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        path = tmp_path / "output" / "backup.sh"
        assert path.exists()
        content = path.read_text()
        assert "#!/" in content
        assert "pg_dump" in content or "backup" in content.lower()

    def test_db_reset_sh_rendered(self, tmp_path):
        """db_reset.sh is generated."""
        config = _make_full_config(tmp_path)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        path = tmp_path / "output" / "db_reset.sh"
        assert path.exists()
        content = path.read_text()
        assert "#!/" in content

    def test_db_deploy_sh_rendered(self, tmp_path):
        """db_deploy.sh is generated."""
        config = _make_full_config(tmp_path)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        path = tmp_path / "output" / "db_deploy.sh"
        assert path.exists()
        content = path.read_text()
        assert "#!/" in content
        assert "confiture" in content or "migrate" in content.lower()


class TestScaffoldCLI:
    """fraisier scaffold generates all files."""

    def test_scaffold_command_generates_files(self, tmp_path):
        """fraisier scaffold generates files to output_dir."""
        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCAFFOLD_YAML.format(output=str(tmp_path / "output")))

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold"])
        assert result.exit_code == 0
        assert (tmp_path / "output").exists()

    def test_scaffold_dry_run(self, tmp_path):
        """fraisier scaffold --dry-run shows files without writing."""
        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCAFFOLD_YAML.format(output=str(tmp_path / "output")))

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold", "--dry-run"])
        assert result.exit_code == 0
        assert not (tmp_path / "output").exists()
        assert "would generate" in result.output.lower() or len(result.output) > 0

    def test_scaffold_gateway_generated_for_multi_fraise(self, tmp_path):
        """Gateway templates generated when >1 fraise."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            """
fraises:
  api_a:
    type: api
    environments:
      production:
        worker_count: 2
  api_b:
    type: api
    environments:
      production:
        worker_count: 2
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output"))
        )

        from click.testing import CliRunner

        from fraisier.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold"])
        assert result.exit_code == 0
        # Nginx gateway should be generated for multi-fraise
        gateway = tmp_path / "output" / "nginx" / "gateway.conf"
        assert gateway.exists()

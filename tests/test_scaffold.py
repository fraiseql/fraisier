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
name: tp
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
        config = self._make_config(tmp_path, "name: tp\nfraises: {}\n")
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
name: tp
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
name: tp
fraises: {}
deployment:
  deploy_user: deploy_bot
""",
        )
        sc = config.scaffold
        assert sc.deploy_user == "deploy_bot"

    def test_postgres_logging_config_defaults(self, tmp_path):
        """PostgresLoggingConfig uses sensible defaults (#42)."""
        config = self._make_config(tmp_path, "name: tp\nfraises: {}\n")
        pg = config.scaffold.postgresql
        assert pg.log_min_duration_statement is None
        assert pg.log_statement is None
        assert pg.log_connections is None
        assert pg.deadlock_timeout == "1s"
        assert pg.log_lock_waits is True
        assert pg.log_rotation_age == "1d"
        assert pg.log_rotation_size == "100MB"

    def test_postgres_logging_config_from_yaml(self, tmp_path):
        """scaffold.postgresql parses overrides from YAML (#42)."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises: {}
scaffold:
  postgresql:
    log_min_duration_statement: "200"
    log_statement: mod
    deadlock_timeout: 2s
    log_lock_waits: false
""",
        )
        pg = config.scaffold.postgresql
        assert pg.log_min_duration_statement == "200"
        assert pg.log_statement == "mod"
        assert pg.deadlock_timeout == "2s"
        assert pg.log_lock_waits is False


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
name: tp
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
name: tp
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
name: tp
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
name: tp
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

        svc_path = tmp_path / "output" / "systemd" / "tp_my_api_production.service"
        assert svc_path.exists(), f"Expected {svc_path} to exist"
        content = svc_path.read_text()

        for directive in _REQUIRED_SECURITY_DIRECTIVES:
            assert directive in content, f"Missing security directive: {directive}"

    def test_service_unit_has_correct_exec_start(self, tmp_path):
        """Rendered .service has correct ExecStart with worker count."""
        config = self._make_config(
            tmp_path,
            """
name: tp
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

        svc_path = tmp_path / "output" / "systemd" / "tp_my_api_production.service"
        content = svc_path.read_text()

        assert "ExecStart=" in content
        assert "User=my_app" in content
        assert "MemoryMax=8G" in content

    def test_service_memory_max_uses_default(self, tmp_path):
        """MemoryMax uses scaffold default when not set per-env."""
        config = self._make_config(
            tmp_path,
            """
name: tp
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

        svc_path = tmp_path / "output" / "systemd" / "tp_my_api_development.service"
        content = svc_path.read_text()
        assert "MemoryMax=2G" in content

    def test_logs_directory_mode_defaults_to_0750(self, tmp_path):
        """LogsDirectoryMode defaults to 0750 when LogsDirectory is set (#42)."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        service:
          logs_directory: myapp
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        svc_path = tmp_path / "output" / "systemd" / "tp_my_api_production.service"
        content = svc_path.read_text()
        assert "LogsDirectory=myapp" in content
        assert "LogsDirectoryMode=0750" in content

    def test_logs_directory_mode_explicit_override(self, tmp_path):
        """Explicit LogsDirectoryMode overrides the 0750 default (#42)."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        service:
          logs_directory: myapp
          logs_directory_mode: "0700"
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        svc_path = tmp_path / "output" / "systemd" / "tp_my_api_production.service"
        content = svc_path.read_text()
        assert "LogsDirectoryMode=0700" in content
        assert "LogsDirectoryMode=0750" not in content

    def test_no_logs_directory_omits_mode(self, tmp_path):
        """No LogsDirectory means no LogsDirectoryMode directive (#42)."""
        config = self._make_config(
            tmp_path,
            """
name: tp
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

        svc_path = tmp_path / "output" / "systemd" / "tp_my_api_production.service"
        content = svc_path.read_text()
        assert "LogsDirectoryMode" not in content


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
name: tp
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
name: tp
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
name: tp
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
name: tp
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

    def test_nginx_gateway_has_acme_challenge(self, tmp_path):
        """Port 80 block includes ACME challenge location for Let's Encrypt."""
        config = self._make_config(
            tmp_path,
            """
name: tp
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

        nginx_path = tmp_path / "output" / "nginx" / "gateway.conf"
        content = nginx_path.read_text()
        assert "listen 80;" in content
        assert "/.well-known/acme-challenge/" in content
        assert "root /var/www/html;" in content
        assert "return 301 https://$host$request_uri;" in content

    def test_nginx_config_has_upstream_and_cors(self, tmp_path):
        """Rendered nginx config has upstream, CORS, security headers."""
        config = self._make_config(
            tmp_path,
            """
name: tp
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

    def test_nginx_cors_uses_map_not_if(self, tmp_path):
        """CORS uses map directive instead of if blocks."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 2
scaffold:
  output_dir: {output}
  nginx:
    cors_origins:
      - '^https://app\\.example\\.com$'
      - '^https?://localhost(:[0-9]+)?$'
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "gateway.conf").read_text()
        assert "map $http_origin $cors_origin" in content
        assert "if ($http_origin" not in content
        assert "Access-Control-Allow-Origin $cors_origin" in content


_SCAFFOLD_YAML = """
name: tp
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


class TestSystemdServiceUsesConfig:
    """Issue #1: systemd units must read paths, ports, exec from fraises.yaml."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_working_directory_uses_app_path(self, tmp_path):
        """WorkingDirectory comes from env app_path, not hardcoded /opt/."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  management_api:
    type: api
    environments:
      production:
        app_path: /var/www/management.printoptim.com
        worker_count: 2
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        svc = tmp_path / "output" / "systemd" / "tp_management_api_production.service"
        content = svc.read_text()
        assert "WorkingDirectory=/var/www/management.printoptim.com" in content
        assert "/opt/management_api" not in content

    def test_port_extracted_from_health_check_url(self, tmp_path):
        """ExecStart port comes from health_check.url, not hardcoded 8000."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  management_api:
    type: api
    environments:
      production:
        app_path: /var/www/management
        worker_count: 2
        health_check:
          url: http://127.0.0.1:8042/health
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        svc = tmp_path / "output" / "systemd" / "tp_management_api_production.service"
        content = svc.read_text()
        assert "--port 8042" in content
        assert "--port 8000" not in content

    def test_exec_command_overrides_default_uvicorn(self, tmp_path):
        """exec_command on fraise replaces default uvicorn ExecStart."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  graphql_gateway:
    type: api
    exec_command: /usr/local/bin/fraiseql-cli serve --port 4000
    environments:
      production:
        app_path: /var/www/graphql
        worker_count: 1
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        svc = tmp_path / "output" / "systemd" / "tp_graphql_gateway_production.service"
        content = svc.read_text()
        assert "ExecStart=/usr/local/bin/fraiseql-cli serve --port 4000" in content
        assert "uvicorn" not in content

    def test_defaults_when_no_app_path_or_health_check(self, tmp_path):
        """Falls back to /opt/<name> and port 8000 when not configured."""
        config = self._make_config(
            tmp_path,
            """
name: tp
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

        svc = tmp_path / "output" / "systemd" / "tp_my_api_production.service"
        content = svc.read_text()
        assert "WorkingDirectory=/opt/my_api" in content
        assert "--port 8000" in content


class TestNginxPerFraiseRouting:
    """Issue #2: nginx must not generate duplicate location / blocks."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_multi_fraise_no_duplicate_location_root(self, tmp_path):
        """Multiple API fraises must NOT produce duplicate location / blocks."""
        config = self._make_config(
            tmp_path,
            """
name: tp
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
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        nginx = tmp_path / "output" / "nginx" / "gateway.conf"
        content = nginx.read_text()
        # Should NOT have multiple "location /" — should use /api_a/ and /api_b/
        assert content.count("location /") >= 2
        assert "location /api_a/" in content
        assert "location /api_b/" in content

    def test_multi_fraise_distinct_upstream_ports(self, tmp_path):
        """Each upstream uses the fraise's own port from health_check.url."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api_a:
    type: api
    environments:
      production:
        app_path: /var/www/api_a
        health_check:
          url: http://127.0.0.1:8001/health
  api_b:
    type: api
    environments:
      production:
        app_path: /var/www/api_b
        health_check:
          url: http://127.0.0.1:8002/health
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        nginx = tmp_path / "output" / "nginx" / "gateway.conf"
        content = nginx.read_text()
        assert "127.0.0.1:8001" in content
        assert "127.0.0.1:8002" in content
        assert content.count("127.0.0.1:8000") == 0

    def test_server_name_generates_separate_server_blocks(self, tmp_path):
        """Fraises with server_name get their own server {} blocks."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  management_api:
    type: api
    server_name: management.example.com
    environments:
      production:
        app_path: /var/www/management
        health_check:
          url: http://127.0.0.1:8042/health
  backend_api:
    type: api
    server_name: backend.example.com
    environments:
      production:
        app_path: /var/www/backend
        health_check:
          url: http://127.0.0.1:8043/health
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        nginx = tmp_path / "output" / "nginx" / "gateway.conf"
        content = nginx.read_text()
        assert "server_name management.example.com" in content
        assert "server_name backend.example.com" in content
        assert "tp_management_api_backend" in content
        assert "tp_backend_api_backend" in content

    def test_single_fraise_uses_location_root(self, tmp_path):
        """Single API fraise still gets location / (no prefix needed)."""
        config = self._make_config(
            tmp_path,
            """
name: tp
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

        nginx = tmp_path / "output" / "nginx" / "gateway.conf"
        content = nginx.read_text()
        assert "location / {" in content
        assert "proxy_pass http://tp_my_api_backend" in content

    def test_custom_location_prefix(self, tmp_path):
        """Fraises with explicit location field use that path."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  management_api:
    type: api
    location: /api/management/
    environments:
      production:
        worker_count: 2
  backend_api:
    type: api
    location: /api/backend/
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

        nginx = tmp_path / "output" / "nginx" / "gateway.conf"
        content = nginx.read_text()
        assert "location /api/management/" in content
        assert "location /api/backend/" in content


class TestSystemdServiceEnvConfig:
    """Issue #4: per-environment service config in systemd units."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def _render_service(
        self, tmp_path, yaml_content, fraise="my_api", env="production"
    ):
        config = self._make_config(tmp_path, yaml_content)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()
        svc = tmp_path / "output" / "systemd" / f"tp_{fraise}_{env}.service"
        return svc.read_text()

    def test_user_group_override(self, tmp_path):
        """service.user and service.group override scaffold.deploy_user."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        service:
          user: myapp_user
          group: www-data
scaffold:
  output_dir: {output}
  deploy_user: fraisier
""".format(output=str(tmp_path / "output")),
        )
        assert "User=myapp_user" in content
        assert "Group=www-data" in content
        assert "User=fraisier" not in content
        assert "Group=fraisier" not in content

    def test_user_group_fallback_to_deploy_user(self, tmp_path):
        """Without service.user/group, falls back to scaffold.deploy_user."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 2
scaffold:
  output_dir: {output}
  deploy_user: my_app
""".format(output=str(tmp_path / "output")),
        )
        assert "User=my_app" in content
        assert "Group=my_app" in content

    def test_memory_high(self, tmp_path):
        """service.memory_high renders MemoryHigh directive."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        service:
          memory_high: "3G"
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "MemoryHigh=3G" in content

    def test_memory_high_absent_when_not_configured(self, tmp_path):
        """MemoryHigh is absent when service.memory_high is not set."""
        content = self._render_service(
            tmp_path,
            """
name: tp
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
        assert "MemoryHigh" not in content

    def test_cpu_quota(self, tmp_path):
        """service.cpu_quota renders CPUQuota directive."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        service:
          cpu_quota: "200%"
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "CPUQuota=200%" in content

    def test_cpu_quota_absent_when_not_configured(self, tmp_path):
        """CPUQuota is absent when service.cpu_quota is not set."""
        content = self._render_service(
            tmp_path,
            """
name: tp
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
        assert "CPUQuota" not in content

    def test_environment_file(self, tmp_path):
        """service.environment_file renders EnvironmentFile directive."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        service:
          environment_file: /etc/myapp/api.env
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "EnvironmentFile=/etc/myapp/api.env" in content

    def test_load_credential(self, tmp_path):
        """service.credentials renders LoadCredential directives."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        service:
          credentials:
            pg_password: /etc/creds/pg
            api_key: /etc/creds/api
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "LoadCredential=pg_password:/etc/creds/pg" in content
        assert "LoadCredential=api_key:/etc/creds/api" in content

    def test_extra_environment_vars(self, tmp_path):
        """service.environment renders extra Environment lines."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        service:
          environment:
            DB_NAME: myapp_db
            REDIS_URL: redis://localhost
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "Environment=DB_NAME=myapp_db" in content
        assert "Environment=REDIS_URL=redis://localhost" in content
        # Built-in env vars still present
        assert "Environment=ENVIRONMENT=production" in content

    def test_security_override(self, tmp_path):
        """service.security overrides individual security directives."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        service:
          security:
            protect_home: "read-only"
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "ProtectHome=read-only" in content
        assert "ProtectHome=true" not in content
        # Other defaults still present
        assert "NoNewPrivileges=true" in content
        assert "ProtectSystem=strict" in content

    def test_port_from_service_overrides_health_check(self, tmp_path):
        """service.port takes precedence over health_check.url port."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        health_check:
          url: http://127.0.0.1:8042/health
        service:
          port: 9000
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "--port 9000" in content
        assert "--port 8042" not in content

    def test_port_fallback_to_health_check(self, tmp_path):
        """Without service.port, port comes from health_check.url."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        health_check:
          url: http://127.0.0.1:8042/health
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "--port 8042" in content

    def test_port_fallback_to_default(self, tmp_path):
        """Without service.port or health_check, port defaults to 8000."""
        content = self._render_service(
            tmp_path,
            """
name: tp
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
        assert "--port 8000" in content

    def test_service_type_configurable(self, tmp_path):
        """service.type overrides default Type=notify."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        service:
          type: exec
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "Type=exec" in content
        assert "Type=notify" not in content

    def test_service_type_defaults_to_notify(self, tmp_path):
        """Without service.type, defaults to Type=notify."""
        content = self._render_service(
            tmp_path,
            """
name: tp
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
        assert "Type=notify" in content

    def test_service_type_invalid_raises(self, tmp_path):
        """Invalid service.type raises ValidationError."""
        import pytest

        from fraisier.config import ServiceConfig

        with pytest.raises(Exception, match=r"service\.type"):
            ServiceConfig(type="bogus")

    def test_exec_start_pre(self, tmp_path):
        """service.exec_start_pre renders ExecStartPre directives."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        service:
          exec_start_pre:
            - "/bin/sh -c 'echo hello'"
            - "/usr/bin/env-gen /run/myapp/pg.env"
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "ExecStartPre=/bin/sh -c 'echo hello'" in content
        assert "ExecStartPre=/usr/bin/env-gen /run/myapp/pg.env" in content

    def test_exec_start_pre_absent_when_not_configured(self, tmp_path):
        """ExecStartPre is absent when not configured."""
        content = self._render_service(
            tmp_path,
            """
name: tp
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
        assert "ExecStartPre" not in content

    def test_runtime_directory(self, tmp_path):
        """service.runtime_directory renders RuntimeDirectory directive."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        service:
          runtime_directory: myapp
          runtime_directory_mode: "0755"
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "RuntimeDirectory=myapp" in content
        assert "RuntimeDirectoryMode=0755" in content

    def test_logs_directory(self, tmp_path):
        """service.logs_directory renders LogsDirectory directive."""
        content = self._render_service(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/app
        service:
          logs_directory: myapp
          logs_directory_mode: "0755"
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "LogsDirectory=myapp" in content
        assert "LogsDirectoryMode=0755" in content

    def test_runtime_logs_directory_absent_when_not_configured(self, tmp_path):
        """RuntimeDirectory/LogsDirectory absent when not configured."""
        content = self._render_service(
            tmp_path,
            """
name: tp
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
        assert "RuntimeDirectory" not in content
        assert "LogsDirectory" not in content


class TestNginxPerEnvConfig:
    """Issue #4: per-environment nginx config files."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_per_env_nginx_has_acme_redirect(self, tmp_path):
        """Per-env nginx includes port 80 ACME challenge + HTTPS redirect."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        nginx:
          server_name: api.myapp.io
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "tp_api_production.conf").read_text()
        assert "listen 80;" in content
        assert "server_name api.myapp.io" in content
        assert "/.well-known/acme-challenge/" in content
        assert "return 301 https://$host$request_uri;" in content

    def test_per_env_nginx_files_generated(self, tmp_path):
        """Environments with nginx: blocks get their own config files."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      development:
        app_path: /var/www/api-dev
        nginx:
          server_name: api.myapp.dev
      production:
        app_path: /var/www/api
        nginx:
          server_name: api.myapp.io
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        files = renderer.render()

        assert "nginx/tp_api_development.conf" in files
        assert "nginx/tp_api_production.conf" in files

        dev_conf = (
            tmp_path / "output" / "nginx" / "tp_api_development.conf"
        ).read_text()
        assert "server_name api.myapp.dev" in dev_conf

        prod_conf = (
            tmp_path / "output" / "nginx" / "tp_api_production.conf"
        ).read_text()
        assert "server_name api.myapp.io" in prod_conf

    def test_per_env_custom_ssl_paths(self, tmp_path):
        """Per-env nginx uses custom SSL cert/key paths."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        nginx:
          server_name: api.myapp.io
          ssl_cert: /etc/ssl/custom/cert.pem
          ssl_key: /etc/ssl/custom/key.pem
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "tp_api_production.conf").read_text()
        assert "ssl_certificate /etc/ssl/custom/cert.pem" in content
        assert "ssl_certificate_key /etc/ssl/custom/key.pem" in content
        assert "letsencrypt" not in content

    def test_per_env_letsencrypt_fallback(self, tmp_path):
        """Without custom SSL paths, uses letsencrypt convention."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        nginx:
          server_name: api.myapp.io
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "tp_api_production.conf").read_text()
        assert "/etc/letsencrypt/live/api.myapp.io/fullchain.pem" in content
        assert "/etc/letsencrypt/live/api.myapp.io/privkey.pem" in content

    def test_per_env_cors_uses_map_not_if(self, tmp_path):
        """Per-env CORS uses map directive instead of if blocks."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        nginx:
          server_name: api.myapp.io
          cors_origins:
            - '^https://app\\.myapp\\.io$'
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "tp_api_production.conf").read_text()
        assert "map $http_origin $cors_origin" in content
        assert "if ($http_origin" not in content
        assert "Access-Control-Allow-Origin $cors_origin" in content

    def test_per_env_cors_origins(self, tmp_path):
        """Per-env cors_origins used instead of global ones."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        nginx:
          server_name: api.myapp.io
          cors_origins:
            - https://app.myapp.io
scaffold:
  output_dir: {output}
  nginx:
    cors_origins: ["https://global.example.com"]
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "tp_api_production.conf").read_text()
        assert r"https://app\.myapp\.io" in content
        assert "global" not in content

    def test_per_env_cors_falls_back_to_global(self, tmp_path):
        """Without per-env cors_origins, global ones are used."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        nginx:
          server_name: api.myapp.io
scaffold:
  output_dir: {output}
  nginx:
    cors_origins: ["https://global.example.com"]
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "tp_api_production.conf").read_text()
        assert r"https://global\.example\.com" in content

    def test_per_env_structured_restricted_paths(self, tmp_path):
        """Per-env restricted_paths with allow/deny rules."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        nginx:
          server_name: api.myapp.io
          restricted_paths:
            - path: /admin/
              allow: ["10.0.0.0/8", "127.0.0.1"]
              deny: all
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "tp_api_production.conf").read_text()
        assert "location /admin/" in content
        assert "allow 10.0.0.0/8;" in content
        assert "allow 127.0.0.1;" in content
        assert "deny all;" in content

    def test_no_per_env_nginx_when_no_nginx_key(self, tmp_path):
        """Without nginx: key, no per-env nginx files generated."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
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
        files = renderer.render()

        # gateway.conf still generated
        assert "nginx/gateway.conf" in files
        # No per-env file
        per_env = [
            f for f in files if f.startswith("nginx/") and f != "nginx/gateway.conf"
        ]
        assert per_env == []

    def test_per_env_upstream_port_from_service(self, tmp_path):
        """Per-env nginx upstream uses service.port."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        service:
          port: 9000
        nginx:
          server_name: api.myapp.io
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "tp_api_production.conf").read_text()
        assert "127.0.0.1:9000" in content

    def test_dry_run_includes_per_env_nginx(self, tmp_path):
        """Dry-run lists per-env nginx files."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        nginx:
          server_name: api.myapp.io
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        files = renderer.render(dry_run=True)

        assert "nginx/tp_api_production.conf" in files
        assert not (tmp_path / "output").exists()


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

    def test_sudoers_uses_per_env_deploy_user(self, tmp_path):
        """Per-env deploy_user overrides scaffold.deploy_user in sudoers (#28)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: tp
scaffold:
  deploy_user: default-deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      development:
        app_path: /var/www/dev
      production:
        app_path: /var/www/prod
        deploy_user: prod-deployer
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        # Development uses default deploy_user
        assert "default-deployer" in content
        assert "my_api_development" in content
        # Production uses per-env deploy_user
        assert "prod-deployer" in content
        assert "my_api_production" in content
        # Per-env command lines use prod-deployer, not default-deployer
        for line in content.splitlines():
            if "my_api_production" in line and not line.startswith("#"):
                assert "prod-deployer" in line

    def test_sudoers_service_names_include_project_prefix(self, tmp_path):
        """Service names in sudoers must match renderer: {project}_{fraise}_{env}."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        # Must use project-prefixed service name
        assert "myproj_my_api_production.service" in content
        # Must NOT use unprefixed service name
        for line in content.splitlines():
            if "systemctl" in line and "daemon-reload" not in line:
                assert "myproj_my_api_production" in line

    def test_sudoers_includes_journalctl(self, tmp_path):
        """Sudoers grants journalctl access for each service (#41)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
      development:
        app_path: /var/www/dev
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        assert "journalctl" in content
        assert "myproj_my_api_production.service" in content
        assert "myproj_my_api_development.service" in content
        # Each env gets its own journalctl rule
        journal_lines = [line for line in content.splitlines() if "journalctl" in line]
        assert len(journal_lines) >= 2

    def test_sudoers_includes_wrapper_for_rebuild_strategy(self, tmp_path):
        """Sudoers grants wrapper access for rebuild database strategy (#41)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      development:
        app_path: /var/www/dev
        database:
          name: myapp_dev
          strategy: rebuild
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        assert "ALL=(postgres)" in content
        assert "/usr/local/libexec/fraisier/pgadmin-myproj" in content

    def test_sudoers_includes_wrapper_for_restore_migrate(self, tmp_path):
        """Sudoers grants wrapper access for restore_migrate strategy (#41)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /var/www/staging
        database:
          name: myapp_staging
          strategy: restore_migrate
          restore:
            backup_dir: /backup/prod
            backup_pattern: "*.dump"
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        assert "ALL=(postgres)" in content
        assert "/usr/local/libexec/fraisier/pgadmin-myproj" in content

    def test_sudoers_no_db_admin_for_migrate_strategy(self, tmp_path):
        """Sudoers omits DB admin commands for migrate/apply strategies (#41)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
        database:
          name: myapp_prod
          strategy: migrate
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        assert "createdb" not in content
        assert "dropdb" not in content
        assert "pg_restore" not in content
        assert "pgadmin" not in content

    def test_sudoers_wrapper_scoped_to_deploy_user(self, tmp_path):
        """Wrapper rules use the correct deploy_user per environment (#41)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: default-deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      development:
        app_path: /var/www/dev
        deploy_user: dev-deployer
        database:
          name: myapp_dev
          strategy: rebuild
      production:
        app_path: /var/www/prod
        database:
          name: myapp_prod
          strategy: migrate
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        # dev-deployer gets wrapper access (rebuild strategy)
        wrapper_lines = [line for line in content.splitlines() if "pgadmin" in line]
        assert any("dev-deployer" in line for line in wrapper_lines)
        # default-deployer (production) does NOT get wrapper (migrate strategy)
        assert not any(
            "default-deployer" in line and "pgadmin" in line
            for line in content.splitlines()
        )

    def test_pg_wrapper_generated_with_allowlist(self, tmp_path):
        """pg-wrapper.sh embeds allowed database names from config (#41)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      development:
        app_path: /var/www/dev
        database:
          name: myapp_dev
          strategy: rebuild
      staging:
        app_path: /var/www/staging
        database:
          name: myapp_staging
          strategy: restore_migrate
          restore:
            backup_dir: /backup/prod
            backup_pattern: "*.dump"
      production:
        app_path: /var/www/prod
        database:
          name: myapp_prod
          strategy: migrate
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        wrapper_path = tmp_path / "output" / "pg-wrapper.sh"
        assert wrapper_path.exists()
        content = wrapper_path.read_text()
        # Allowlist includes only databases with admin strategies
        assert "myapp_dev" in content
        assert "myapp_staging" in content
        # Production (migrate) must NOT be in the allowlist
        assert "myapp_prod" not in content
        # Template databases are also allowed
        assert "template_myapp_dev" in content
        assert "template_myapp_staging" in content
        # Script is executable-ready
        assert content.startswith("#!/")

    def test_pg_wrapper_includes_custom_template_names(self, tmp_path):
        """pg-wrapper.sh includes custom template_name from config (#41)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /var/www/staging
        database:
          name: myapp_staging
          strategy: restore_migrate
          restore:
            backup_dir: /backup/prod
            backup_pattern: "*.dump"
            template_name: myapp_staging_tpl
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "pg-wrapper.sh").read_text()
        assert "myapp_staging_tpl" in content

    def test_pg_wrapper_not_generated_without_admin_strategies(self, tmp_path):
        """pg-wrapper.sh is not generated when no env needs DB admin (#41)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
        database:
          name: myapp_prod
          strategy: migrate
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        files = renderer.render()
        assert "pg-wrapper.sh" not in files

    def test_sudoers_references_wrapper_for_db_admin(self, tmp_path):
        """Sudoers uses wrapper path instead of raw pg commands (#41)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      development:
        app_path: /var/www/dev
        database:
          name: myapp_dev
          strategy: rebuild
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        # Should reference the wrapper, not raw psql/createdb/dropdb
        assert "/usr/local/libexec/fraisier/pgadmin-myproj" in content
        # Raw commands should NOT appear in sudoers
        assert "NOPASSWD: /usr/bin/psql" not in content
        assert "NOPASSWD: /usr/bin/createdb" not in content
        assert "NOPASSWD: /usr/bin/dropdb" not in content

    def test_sudoers_includes_install_command_for_app_user(self, tmp_path):
        """Sudoers allows deploy_user to run install as app user (#44)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: fraisier
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        install:
          command: [/home/myapp/.local/bin/uv, sync, --frozen]
          user: myapp
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        assert "ALL=(myapp)" in content
        assert "/home/myapp/.local/bin/uv sync --frozen" in content

    def test_sudoers_omits_install_when_no_user(self, tmp_path):
        """Sudoers omits install rule when install.user is not set (#44)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: fraisier
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        install:
          command: [uv, sync, --frozen]
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        # No install rule since no user is specified
        assert "Dependency install" not in content

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

    def test_install_sh_creates_app_users(self, tmp_path):
        """install.sh creates app users when service.user is set (#28)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: tp
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        service:
          user: myapp
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "install.sh").read_text()
        assert "myapp" in content
        assert "Creating app user myapp" in content

    def test_install_sh_service_names_include_project_prefix(self, tmp_path):
        """install.sh copies service files with project-prefixed names."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "install.sh").read_text()
        # Must use project-prefixed service name
        assert "myproj_my_api_production.service" in content
        # Must NOT use unprefixed service name in cp commands
        for line in content.splitlines():
            if "cp " in line and ".service" in line:
                assert "myproj_" in line

    def test_webhook_service_sets_pg_wrapper_env(self, tmp_path):
        """Webhook service sets FRAISIER_PG_WRAPPER when wrapper exists."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      development:
        app_path: /var/www/dev
        database:
          name: myapp_dev
          strategy: rebuild
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "fraisier-webhook.service").read_text()
        assert "FRAISIER_PG_WRAPPER=" in content
        assert "/usr/local/libexec/fraisier/pgadmin-myproj" in content

    def test_webhook_service_no_pg_wrapper_without_admin_strategies(self, tmp_path):
        """Webhook service omits FRAISIER_PG_WRAPPER when no admin strategies."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
        database:
          name: myapp_prod
          strategy: migrate
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "fraisier-webhook.service").read_text()
        assert "FRAISIER_PG_WRAPPER" not in content


class TestPostgresLogging:
    """PostgreSQL logging config generation (#42)."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_pg_logging_generated_when_database_present(self, tmp_path):
        """postgresql/ configs generated when any fraise has a database."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        database:
          name: myapp_prod
          strategy: migrate
""",
        )
        renderer = ScaffoldRenderer(config)
        files = renderer.render()
        assert "postgresql/fraisier_production.conf" in files
        pg_conf = tmp_path / "output" / "postgresql" / "fraisier_production.conf"
        assert pg_conf.exists()

    def test_pg_logging_not_generated_without_database(self, tmp_path):
        """No postgresql/ configs when no fraise has a database section."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
""",
        )
        renderer = ScaffoldRenderer(config)
        files = renderer.render()
        assert not any(f.startswith("postgresql/") for f in files)

    def test_pg_logging_dev_defaults(self, tmp_path):
        """Development env uses log_statement=all, 100ms threshold, connections on."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      development:
        database:
          name: myapp_dev
          strategy: rebuild
""",
        )
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (
            tmp_path / "output" / "postgresql" / "fraisier_development.conf"
        ).read_text()
        assert "log_min_duration_statement = 100" in content
        assert "log_statement = 'all'" in content
        assert "log_connections = on" in content

    def test_pg_logging_production_defaults(self, tmp_path):
        """Production env uses log_statement=ddl, 500ms threshold, connections off."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        database:
          name: myapp_prod
          strategy: migrate
""",
        )
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (
            tmp_path / "output" / "postgresql" / "fraisier_production.conf"
        ).read_text()
        assert "log_min_duration_statement = 500" in content
        assert "log_statement = 'ddl'" in content
        assert "log_connections = off" in content

    def test_pg_logging_override_wins(self, tmp_path):
        """scaffold.postgresql overrides win over env defaults."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
  postgresql:
    log_min_duration_statement: "200"
    log_statement: mod
fraises:
  my_api:
    type: api
    environments:
      production:
        database:
          name: myapp_prod
          strategy: migrate
""",
        )
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (
            tmp_path / "output" / "postgresql" / "fraisier_production.conf"
        ).read_text()
        assert "log_min_duration_statement = 200" in content
        assert "log_statement = 'mod'" in content

    def test_pg_logging_unknown_env_uses_production(self, tmp_path):
        """Unknown environment names fall back to production defaults."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      qa:
        database:
          name: myapp_qa
          strategy: migrate
""",
        )
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "postgresql" / "fraisier_qa.conf").read_text()
        assert "log_min_duration_statement = 500" in content
        assert "log_statement = 'ddl'" in content

    def test_pg_logging_per_env_files(self, tmp_path):
        """One config file generated per unique environment name."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      development:
        database:
          name: myapp_dev
          strategy: rebuild
      production:
        database:
          name: myapp_prod
          strategy: migrate
""",
        )
        renderer = ScaffoldRenderer(config)
        files = renderer.render()
        assert "postgresql/fraisier_development.conf" in files
        assert "postgresql/fraisier_production.conf" in files

    def test_install_sh_mentions_pg_config_when_database(self, tmp_path):
        """install.sh mentions PostgreSQL logging when databases exist."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        database:
          name: myapp_prod
          strategy: migrate
""",
        )
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "install.sh").read_text()
        assert "postgresql" in content.lower()

    def test_install_sh_no_pg_mention_without_database(self, tmp_path):
        """install.sh omits PostgreSQL logging instructions without databases."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
""",
        )
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "install.sh").read_text()
        assert "conf.d" not in content


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


class TestPerEnvIntegration:
    """Issue #4: full integration tests for per-env service + nginx config."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_full_round_trip_all_new_fields(self, tmp_path):
        """Comprehensive YAML with all new fields renders all files correctly."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      development:
        app_path: /var/www/api-dev
        service:
          user: myapp_dev
          group: www-data
          port: 8000
          workers: 2
          memory_max: "2G"
          memory_high: "1G"
          environment_file: /etc/myapp/dev.env
          credentials:
            pg_password: /etc/creds/pg_dev
          environment:
            DB_NAME: myapp_dev
          security:
            protect_home: "read-only"
        nginx:
          server_name: api.dev.example.com
          cors_origins: ["https://app.dev.example.com"]
      production:
        app_path: /var/www/api
        service:
          user: myapp_prod
          group: www-data
          port: 8000
          workers: 4
          memory_max: "8G"
          memory_high: "6G"
          cpu_quota: "200%"
          environment_file: /etc/myapp/prod.env
          credentials:
            pg_password: /etc/creds/pg_prod
            api_key: /etc/creds/api_key
          environment:
            DB_NAME: myapp_prod
            REDIS_URL: redis://localhost
        nginx:
          server_name: api.example.com
          ssl_cert: /etc/ssl/api/cert.pem
          ssl_key: /etc/ssl/api/key.pem
          cors_origins: ["https://app.example.com"]
          restricted_paths:
            - path: /admin/
              allow: ["10.0.0.0/8"]
              deny: all
  worker:
    type: etl
    environments:
      production:
        app_path: /var/www/worker
        service:
          user: worker_user
          workers: 1
          memory_max: "4G"
scaffold:
  output_dir: {output}
  deploy_user: fallback_user
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        files = renderer.render()

        # Systemd files for all fraise+env combos
        assert "systemd/tp_api_development.service" in files
        assert "systemd/tp_api_production.service" in files
        assert "systemd/tp_worker_production.service" in files

        # Per-env nginx for api (has nginx: blocks)
        assert "nginx/tp_api_development.conf" in files
        assert "nginx/tp_api_production.conf" in files

        # No per-env nginx for worker (no nginx: block)
        worker_nginx = [f for f in files if f.startswith("nginx/tp_worker_")]
        assert worker_nginx == []

        # Verify dev systemd content
        dev_svc = (
            tmp_path / "output" / "systemd" / "tp_api_development.service"
        ).read_text()
        assert "User=myapp_dev" in dev_svc
        assert "Group=www-data" in dev_svc
        assert "MemoryMax=2G" in dev_svc
        assert "MemoryHigh=1G" in dev_svc
        assert "EnvironmentFile=/etc/myapp/dev.env" in dev_svc
        assert "LoadCredential=pg_password:/etc/creds/pg_dev" in dev_svc
        assert "Environment=DB_NAME=myapp_dev" in dev_svc
        assert "ProtectHome=read-only" in dev_svc

        # Verify prod systemd content
        prod_svc = (
            tmp_path / "output" / "systemd" / "tp_api_production.service"
        ).read_text()
        assert "User=myapp_prod" in prod_svc
        assert "CPUQuota=200%" in prod_svc
        assert "LoadCredential=api_key:/etc/creds/api_key" in prod_svc
        assert "Environment=REDIS_URL=redis://localhost" in prod_svc

        # Verify worker uses fallback deploy_user
        worker_svc = (
            tmp_path / "output" / "systemd" / "tp_worker_production.service"
        ).read_text()
        assert "User=worker_user" in worker_svc

        # Verify prod nginx content
        prod_nginx = (
            tmp_path / "output" / "nginx" / "tp_api_production.conf"
        ).read_text()
        assert "server_name api.example.com" in prod_nginx
        assert "ssl_certificate /etc/ssl/api/cert.pem" in prod_nginx
        assert r"https://app\.example\.com" in prod_nginx
        assert "location /admin/" in prod_nginx
        assert "allow 10.0.0.0/8;" in prod_nginx

    def test_mixed_new_and_legacy_config(self, tmp_path):
        """One fraise uses service: blocks, another uses flat fields."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  new_style:
    type: api
    environments:
      production:
        app_path: /var/www/new
        service:
          user: new_user
          workers: 4
          memory_max: "8G"
  legacy_style:
    type: api
    environments:
      production:
        app_path: /var/www/legacy
        worker_count: 2
        memory_max: "4G"
        exec_command: /usr/bin/custom-server
scaffold:
  output_dir: {output}
  deploy_user: default_user
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        new_svc = (
            tmp_path / "output" / "systemd" / "tp_new_style_production.service"
        ).read_text()
        assert "User=new_user" in new_svc
        assert "--workers 4" in new_svc
        assert "MemoryMax=8G" in new_svc

        legacy_svc = (
            tmp_path / "output" / "systemd" / "tp_legacy_style_production.service"
        ).read_text()
        assert "User=default_user" in legacy_svc
        assert "MemoryMax=4G" in legacy_svc
        assert "ExecStart=/usr/bin/custom-server" in legacy_svc


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
name: tp
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

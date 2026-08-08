"""Infrastructure scaffold tests."""

import pytest

from fraisier.config import FraisierConfig
from fraisier.errors import ValidationError


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
    cors_origins:
      - {pattern: "*.example.io", type: wildcard}
      - {pattern: "localhost:*", type: literal}
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
        # CORS origins are auto-processed for nginx regex
        assert "^[^.]+\\.example\\.io$" in sc.nginx.cors_origins_escaped
        assert "localhost:*" in sc.nginx.cors_origins_escaped
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


class TestSyncPairConfig:
    """scaffold.sync pairs parse correctly and generate sync scripts."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_sync_pairs_parse(self, tmp_path):
        """scaffold.sync parses source/target pairs."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises: {}
scaffold:
  sync:
    - source: dev
      target: staging
    - source: staging
      target: production
""",
        )
        pairs = config.scaffold.sync
        assert len(pairs) == 2
        assert pairs[0].source == "dev"
        assert pairs[0].target == "staging"
        assert pairs[1].source == "staging"
        assert pairs[1].target == "production"

    def test_sync_pairs_prefer_source_true(self, tmp_path):
        """scaffold.sync parses prefer_source: true."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises: {}
scaffold:
  sync:
    - source: dev
      target: staging
      prefer_source: true
""",
        )
        pairs = config.scaffold.sync
        assert len(pairs) == 1
        assert pairs[0].prefer_source is True

    def test_sync_pairs_prefer_source_false(self, tmp_path):
        """scaffold.sync parses prefer_source: false."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises: {}
scaffold:
  sync:
    - source: dev
      target: staging
      prefer_source: false
""",
        )
        pairs = config.scaffold.sync
        assert len(pairs) == 1
        assert pairs[0].prefer_source is False

    def test_sync_pairs_prefer_source_default(self, tmp_path):
        """scaffold.sync defaults prefer_source to False when absent."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises: {}
scaffold:
  sync:
    - source: dev
      target: staging
""",
        )
        pairs = config.scaffold.sync
        assert len(pairs) == 1
        assert pairs[0].prefer_source is False

    def test_sync_defaults_to_empty(self, tmp_path):
        """scaffold.sync defaults to empty list when not configured."""
        config = self._make_config(tmp_path, "name: tp\nfraises: {}\n")
        assert config.scaffold.sync == []


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

    def test_service_unit_includes_watchdog_sec_when_configured(self, tmp_path):
        """Rendered .service includes WatchdogSec when configured."""
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
          server_type: gunicorn
          watchdog_sec: 30s
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

        assert "WatchdogSec=30s" in content

    def test_service_unit_omits_watchdog_sec_by_default(self, tmp_path):
        """Rendered .service omits WatchdogSec by default."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  my_api:
    type: api
    environments:
      production: {{}}
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

        assert "WatchdogSec=" not in content

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


# ProtectHome is deliberately absent, and its absence is itself asserted by
# tests/test_rendered_unit_sanity.py. This list used to carry `ProtectHome=true`
# and so pinned #341's bug in place as a requirement: both units invoke
# /home/{deploy_user}/.local/bin/fraisier, which any ProtectHome value hides
# (#72, Bug 3). The unit that would have failed on every firing had a passing
# test saying it was correctly hardened.
_DEPLOY_CHECKER_SECURITY_DIRECTIVES = [
    "NoNewPrivileges=true",
    "ProtectSystem=strict",
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

    def test_deploy_checker_service_has_security_directives(self, tmp_path):
        """deploy-checker.service has all security hardening directives."""
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

        svc_path = tmp_path / "output" / "systemd" / "deploy-checker.service"
        assert svc_path.exists()
        content = svc_path.read_text()

        for directive in _DEPLOY_CHECKER_SECURITY_DIRECTIVES:
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

        for directive in _DEPLOY_CHECKER_SECURITY_DIRECTIVES:
            assert directive in content, f"Missing directive: {directive}"
        # `-`-prefixed; see test_backup_service_tolerates_a_missing_backup_dir.
        assert "ReadWritePaths=-/var/backups/" in content

    def test_backup_service_tolerates_a_missing_backup_dir(self, tmp_path):
        """An un-prefixed ReadWritePaths= to a missing path fails unit setup.

        `backup.sh` opens with `mkdir -p "${BACKUP_DIR}"`, which reads as
        "the script creates its own directory" and cannot: systemd builds the
        mount namespace *before* ExecStart, and refuses to start the unit when
        a ReadWritePaths= target does not exist. So on a host that has never
        had /var/backups/{project} — every host, since nothing creates it —
        the unit fails before the script gets to create anything.

        `-` makes the grant advisory; install.sh creating the directory is
        what makes the first run actually write a dump (#341).
        """
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

        content = (tmp_path / "output" / "systemd" / "backup.service").read_text()
        assert "ReadWritePaths=-/var/backups/tp" in content

    def test_install_sh_creates_the_backup_dir(self, tmp_path):
        """The other half: advisory grant plus a directory that exists.

        Provisioned through the PathManifest rather than a bespoke `mkdir` in
        the template, so it is created, owned and mode-set by the same
        `_ensure_dir` as every other managed path — and so `doctor` and the
        provisioners see it too.
        """
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
  deploy_user: deployer
""".format(output=str(tmp_path / "output")),
        )
        renderer = ScaffoldRenderer(config)
        renderer.render()

        install_sh = (tmp_path / "output" / "install.sh").read_text()
        assert '_ensure_dir "/var/backups/tp" "deployer" "deployer"' in install_sh

    def test_backup_service_has_on_failure_alert_hook(self, tmp_path):
        """backup.service emits OnFailure= so operators see backup failures (#202 Phase 4)."""
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
        content = svc_path.read_text()

        assert "OnFailure=fraisier-tp-backup-alert@%n.service" in content

    def test_backup_alert_unit_is_scaffolded(self, tmp_path):
        """backup-alert@.service template renders with a systemd-cat default (#202 Phase 4)."""
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

        alert_path = (
            tmp_path / "output" / "systemd" / "fraisier-tp-backup-alert@.service"
        )
        assert alert_path.exists(), "backup-alert@.service must be scaffolded"
        content = alert_path.read_text()

        # Default action is passive — log to journal via systemd-cat.
        assert "systemd-cat" in content
        assert "fraisier-backup-alert" in content
        # %i identifies the failing unit; OnFailure= passes the failing
        # unit's name via %n which becomes the template instance.
        assert "%i" in content


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
    cors_origins:
      - {{pattern: "*.example.io", type: wildcard}}
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
      - {{pattern: '^https://app\\.example\\.com$', type: regex}}
      - {{pattern: '^https?://localhost(:[0-9]+)?$', type: regex}}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "gateway.conf").read_text()
        assert "map $http_origin $cors_origin" in content
        assert "if ($http_origin" not in content
        assert "Access-Control-Allow-Origin $cors_origin" in content

    def test_restricted_paths_proxy_to_explicit_gateway_fraise(self, tmp_path):
        """restricted_paths proxy_pass uses gateway_fraise when explicitly configured.

        Regression for #146: without gateway_fraise, the restricted_paths block used
        local_fraises[0] which is order-dependent and breaks with multiple fraises.
        """
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  worker:
    type: api
    environments:
      production:
        worker_count: 1
  api:
    type: api
    environments:
      production:
        worker_count: 4
scaffold:
  output_dir: {output}
  nginx:
    restricted_paths: ["/admin/"]
    gateway_fraise: api
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "gateway.conf").read_text()
        # restricted path must proxy to the explicitly configured fraise, not [0]
        assert "location /admin/" in content
        assert "proxy_pass http://tp_api_backend" in content

    def test_restricted_paths_proxy_infers_single_fraise(self, tmp_path):
        """restricted_paths proxy_pass auto-infers the fraise when only one exists."""
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
    restricted_paths: ["/internal/"]
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "gateway.conf").read_text()
        assert "location /internal/" in content
        assert "proxy_pass http://tp_my_api_backend" in content

    def test_multiple_api_fraises_with_restricted_paths_without_gateway_fraise_raises(
        self, tmp_path
    ):
        """restricted_paths + multiple API fraises without gateway_fraise raises ValidationError."""
        import pytest

        from fraisier.errors import ValidationError

        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  api:
    type: api
    environments:
      production:
        worker_count: 4
  worker:
    type: api
    environments:
      production:
        worker_count: 1
scaffold:
  output_dir: {output}
  nginx:
    restricted_paths: ["/admin/"]
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        with pytest.raises(ValidationError, match="gateway_fraise"):
            ScaffoldRenderer(config)


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
    cors_origins:
      - {{pattern: "*.example.io", type: wildcard}}
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
        app_path: /var/www/management.example.com
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
        assert "WorkingDirectory=/var/www/management.example.com" in content
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
    exec_command: /usr/local/bin/myapp-cli serve --port 4000
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
        assert "ExecStart=/usr/local/bin/myapp-cli serve --port 4000" in content
        assert "uvicorn" not in content

    def test_relative_exec_command_gets_app_path_prepended(self, tmp_path):
        """Relative exec_command is made absolute with app_path (#90)."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  myapp:
    type: api
    exec_command: .venv/bin/uvicorn myapp:app --host 0.0.0.0 --port 8000
    environments:
      production:
        app_path: /var/www/myapp
        worker_count: 1
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        svc = tmp_path / "output" / "systemd" / "tp_myapp_production.service"
        content = svc.read_text()
        assert (
            "ExecStart=/var/www/myapp/.venv/bin/uvicorn myapp:app"
            " --host 0.0.0.0 --port 8000" in content
        )

    def test_absolute_exec_command_unchanged(self, tmp_path):
        """Absolute exec_command is not modified."""
        config = self._make_config(
            tmp_path,
            """
name: tp
fraises:
  myapp:
    type: api
    exec_command: /usr/local/bin/custom-server --port 8000
    environments:
      production:
        app_path: /var/www/myapp
        worker_count: 1
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        svc = tmp_path / "output" / "systemd" / "tp_myapp_production.service"
        content = svc.read_text()
        assert "ExecStart=/usr/local/bin/custom-server --port 8000" in content

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

    def test_no_ssl_catchall_when_server_name_only_in_per_env_nginx(self, tmp_path):
        """gateway.conf must not emit any HTTPS blocks when per-env nginx exists.

        Regression for #127 (catch-all suppression) and #197 (multi-server
        safety): when per-env nginx configs exist, each gateway_env.conf is a
        self-contained virtual host.  gateway.conf should contain only the
        shared limit_req_zone and HTTP catch-all — no HTTPS blocks at all.
        """
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
        health_check:
          url: http://127.0.0.1:8080/health
        nginx:
          server_name: api.example.com
          ssl_cert: /etc/ssl/certs/api.crt
          ssl_key: /etc/ssl/private/api.key
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "gateway.conf").read_text()
        # Only the HTTP catch-all should have server_name _;
        assert content.count("server_name _;") == 1
        # No HTTPS blocks — those live in the per-env config now (#197).
        assert "listen 443" not in content
        assert "ssl_certificate" not in content
        # The named SSL block must be in the per-env config instead.
        env_conf = (tmp_path / "output" / "nginx" / "api.example.com.conf").read_text()
        assert "server_name api.example.com" in env_conf

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
        """service.type overrides default Type=exec."""
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
          type: notify
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert "Type=notify" in content
        assert "Type=exec" not in content

    def test_service_type_defaults_to_exec(self, tmp_path):
        """Without service.type, defaults to Type=exec."""
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
        assert "Type=exec" in content

    def test_stop_directives_present(self, tmp_path):
        """KillMode and TimeoutStopSec are always rendered."""
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
        assert "KillMode=control-group" in content
        assert "TimeoutStopSec=10" in content

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

    def test_bytecode_writing_disabled(self, tmp_path):
        """The app unit sets PYTHONDONTWRITEBYTECODE=1 (#292).

        service.user may be a third identity, distinct from both
        scaffold.deploy_user and install.user. Without this the running app
        byte-compiles into app_path/.venv/**/__pycache__ owned by that identity,
        which the install user cannot unlink on the next `uv sync --frozen`.
        """
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
scaffold:
  output_dir: {output}
  deploy_user: fraisier
""".format(output=str(tmp_path / "output")),
        )
        assert "Environment=PYTHONDONTWRITEBYTECODE=1" in content

    def test_bytecode_default_precedes_user_environment(self, tmp_path):
        """A user-supplied override renders after ours, so it wins (#292).

        systemd resolves a repeated Environment= assignment last-wins. Emitting
        the default before service.environment keeps it overridable by anyone
        who measures the per-start compile cost and decides against it.
        """
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
            PYTHONDONTWRITEBYTECODE: "0"
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        assert content.index("Environment=PYTHONDONTWRITEBYTECODE=1") < content.index(
            "Environment=PYTHONDONTWRITEBYTECODE=0"
        )


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

        content = (tmp_path / "output" / "nginx" / "api.myapp.io.conf").read_text()
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

        assert "nginx/api.myapp.dev.conf" in files
        assert "nginx/api.myapp.io.conf" in files

        dev_conf = (tmp_path / "output" / "nginx" / "api.myapp.dev.conf").read_text()
        assert "server_name api.myapp.dev" in dev_conf

        prod_conf = (tmp_path / "output" / "nginx" / "api.myapp.io.conf").read_text()
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

        content = (tmp_path / "output" / "nginx" / "api.myapp.io.conf").read_text()
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

        content = (tmp_path / "output" / "nginx" / "api.myapp.io.conf").read_text()
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
            - {{pattern: '^https://app\\.myapp\\.io$', type: regex}}
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "api.myapp.io.conf").read_text()
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
            - {{pattern: "https://app.myapp.io", type: literal}}
scaffold:
  output_dir: {output}
  nginx:
    cors_origins:
      - {{pattern: "https://global.example.com", type: literal}}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "api.myapp.io.conf").read_text()
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
    cors_origins:
      - {{pattern: "https://global.example.com", type: literal}}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "api.myapp.io.conf").read_text()
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

        content = (tmp_path / "output" / "nginx" / "api.myapp.io.conf").read_text()
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

        content = (tmp_path / "output" / "nginx" / "api.myapp.io.conf").read_text()
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

        assert "nginx/api.myapp.io.conf" in files
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
        """sudoers file is rendered; systemctl goes via socket helper, not sudoers."""
        config = _make_full_config(tmp_path)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        sudoers_path = tmp_path / "output" / "sudoers"
        assert sudoers_path.exists()
        content = sudoers_path.read_text()
        # systemctl is no longer in sudoers — service management goes through
        # the fraisier-systemctl-helper Unix socket (no privilege escalation needed)
        assert "systemctl" not in content
        # The file should still be generated (may be empty when no install rules exist)
        assert "Generated by fraisier scaffold" in content

    def test_sudoers_uses_per_env_deploy_user(self, tmp_path):
        """Per-env deploy_user is used for sudoers install rules (#28).

        Service management now goes via Unix socket helper, not sudoers.
        """
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
        # Systemctl is no longer in sudoers — service management goes through
        # the fraisier-systemctl-helper Unix socket (no sudo needed)
        assert "systemctl" not in content
        assert "libexec/fraisier/systemctl" not in content
        # The sudoers file is still generated (may be empty when no install rules)
        assert "Generated by fraisier scaffold" in content

    def test_sudoers_service_names_include_project_prefix(self, tmp_path):
        """Systemctl helper service uses project prefix in its unit name.

        Service management is now via Unix socket helper, not a sudoers wrapper.
        The helper .service and .socket units are named with the project prefix.
        """
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

        # The helper units are rendered with the project prefix
        helper_svc = (
            tmp_path / "output" / "systemd" / "fraisier-myproj-systemctl-helper.service"
        )
        helper_sock = (
            tmp_path / "output" / "systemd" / "fraisier-myproj-systemctl-helper.socket"
        )
        assert helper_svc.exists()
        assert helper_sock.exists()
        # Socket path uses project prefix
        assert "systemctl-myproj.sock" in helper_sock.read_text()
        # Sudoers no longer contains systemctl wrapper rule
        content = (tmp_path / "output" / "sudoers").read_text()
        assert "systemctl" not in content

    def test_sudoers_wrapper_handles_all_services(self, tmp_path):
        """Helper contains all allowed services; sudoers has no systemctl rule.

        Service management uses the Unix socket helper, not a sudoers wrapper.
        The helper ExecStart includes the allowed service names as arguments.
        The legacy systemctl-wrapper.sh is still generated for backward compat.
        """
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

        # Sudoers no longer contains a systemctl wrapper rule
        content = (tmp_path / "output" / "sudoers").read_text()
        assert "systemctl" not in content

        # Systemctl helper service lists allowed services in ExecStart
        helper_svc = (
            tmp_path / "output" / "systemd" / "fraisier-myproj-systemctl-helper.service"
        )
        helper_content = helper_svc.read_text()
        assert "myproj_my_api_production.service" in helper_content
        assert "myproj_my_api_development.service" in helper_content

        # Legacy wrapper script is still generated (for reference / rollback)
        wrapper_content = (tmp_path / "output" / "systemctl-wrapper.sh").read_text()
        assert "myproj_my_api_production.service" in wrapper_content
        assert "myproj_my_api_development.service" in wrapper_content

    def test_allowed_services_includes_webhook_unit(self, tmp_path):
        """Helper allowlist must include the webhook unit so #162 self-upgrade
        can restart the webhook via the systemctl-helper socket."""
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

        helper_svc = (
            tmp_path / "output" / "systemd" / "fraisier-myproj-systemctl-helper.service"
        )
        helper_content = helper_svc.read_text()
        assert "fraisier-myproj-webhook.service" in helper_content

        # And the legacy wrapper carries it too, so rollbacks keep working.
        wrapper_content = (tmp_path / "output" / "systemctl-wrapper.sh").read_text()
        assert "fraisier-myproj-webhook.service" in wrapper_content

    def test_allowed_services_includes_scheduled_job_units(self, tmp_path):
        """Helper allowlist must include systemd_service AND systemd_timer
        declared on type:scheduled fraises' jobs.* — otherwise the webhook-driven
        ScheduledDeployer cannot enable/restart these units via the helper socket
        (#239). NOTE: this is NOT #218 (which was the webhook unit, fixed in
        v0.22.2); this is a separate, previously-untested gap surfaced by #239.
        """
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  alerter:
    type: scheduled
    environments:
      production:
        app_path: /var/www/api
        jobs:
          poll:
            name: myproj-long-lock-alerter
            systemd_service: myproj-long-lock-alerter.service
            systemd_timer: myproj-long-lock-alerter.timer
            schedule: "*-*-* *:*:00"
"""
        )
        config = FraisierConfig(p)
        ScaffoldRenderer(config).render()

        helper_content = (
            tmp_path / "output" / "systemd" / "fraisier-myproj-systemctl-helper.service"
        ).read_text()
        assert "myproj-long-lock-alerter.service" in helper_content
        assert "myproj-long-lock-alerter.timer" in helper_content

        # Legacy wrapper carries them too — rollbacks must still work.
        wrapper_content = (tmp_path / "output" / "systemctl-wrapper.sh").read_text()
        assert "myproj-long-lock-alerter.service" in wrapper_content
        assert "myproj-long-lock-alerter.timer" in wrapper_content

    def test_systemctl_helper_execstart_carries_deploy_user_flag(self, tmp_path):
        """02 Phase 3 cycle 3.3 — helper unit's ExecStart carries --deploy-user.

        The helper resolves the username to UID at startup via pwd.getpwnam,
        deferring the lookup to the target host where the user is guaranteed
        to exist.
        """
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

        content = (
            tmp_path / "output" / "systemd" / "fraisier-myproj-systemctl-helper.service"
        ).read_text()
        # The flag appears on the ExecStart line, before any positional args.
        execstart = next(
            line for line in content.splitlines() if line.startswith("ExecStart=")
        )
        assert "--deploy-user deployer" in execstart

    def test_scaffold_install_helper_execstart_carries_deploy_user_flag(self, tmp_path):
        """02 Phase 3 cycle 3.3 — scaffold-install-helper carries --deploy-user."""
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

        content = (
            tmp_path
            / "output"
            / "systemd"
            / "fraisier-myproj-scaffold-install-helper.service"
        ).read_text()
        execstart = next(
            line for line in content.splitlines() if line.startswith("ExecStart=")
        )
        assert "--deploy-user deployer" in execstart

    def test_install_helper_execstart_carries_deploy_user_flag(self, tmp_path):
        """02 Phase 3 cycle 3.3 — per-(fraise,env) install-helper carries --deploy-user.

        Even though the install-helper runs as install_user (not deploy_user),
        the SocketUser is deploy_user — that's the UID we check connections
        against.
        """
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
        install:
          user: install_bot
          command: ["uv", "sync", "--frozen"]
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        rendered = list((tmp_path / "output" / "systemd").iterdir())
        candidates = [
            p
            for p in rendered
            if p.name.endswith("-install-helper.service")
            and "scaffold-install-helper" not in p.name
        ]
        assert candidates, f"no install-helper.service rendered (have {rendered})"
        execstart = next(
            line
            for line in candidates[0].read_text().splitlines()
            if line.startswith("ExecStart=")
        )
        assert "--deploy-user deployer" in execstart

    def test_install_helper_relocates_caches_under_app_path(self, tmp_path):
        """The install-helper relocates write-heavy tool dirs under app_path so
        any toolchain works under ProtectSystem=strict (#280).

        No dependency on a writable /home/<user>/.cache: a single writable root
        (app_path) covers the venv plus every relocated cache/state dir.
        """
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
        install:
          user: install_bot
          command: ["bash", "scripts/deploy-install.sh"]
"""
        )
        config = FraisierConfig(p)
        ScaffoldRenderer(config).render()

        svc = next(
            p
            for p in (tmp_path / "output" / "systemd").iterdir()
            if p.name.endswith("-install-helper.service")
            and "scaffold-install-helper" not in p.name
        )
        content = svc.read_text()

        # Caches/state/data all under app_path.
        assert "Environment=XDG_CACHE_HOME=/var/www/prod/.cache" in content
        assert "Environment=XDG_DATA_HOME=/var/www/prod/.local/share" in content
        assert "Environment=XDG_STATE_HOME=/var/www/prod/.local/state" in content
        assert "Environment=UV_CACHE_DIR=/var/www/prod/.cache/uv" in content
        assert "Environment=CARGO_HOME=/var/www/prod/.cargo" in content
        # No dependency on a writable home cache in the sandbox allowlist.
        assert "/home/install_bot/.cache" not in content
        assert "ReadWritePaths=/var/www/prod" in content
        # Still strictly sandboxed.
        assert "ProtectSystem=strict" in content

    def test_collect_allowed_services_skips_jobs_on_non_scheduled_types(self):
        """Only type:scheduled fraises contribute jobs.* unit names to the
        allowlist. type:backup fraises (which also use jobs.*) must NOT —
        their units are managed by a different path."""
        from fraisier.scaffold.renderer import _collect_allowed_services

        fraises = [
            {
                "name": "nightly_backup",
                "type": "backup",
                "environments": {
                    "production": {
                        "app_path": "/var/www/api",
                        "jobs": {
                            "dump": {
                                "name": "myproj-nightly-backup",
                                "systemd_service": "myproj-nightly-backup.service",
                                "systemd_timer": "myproj-nightly-backup.timer",
                            }
                        },
                    }
                },
            }
        ]
        services = _collect_allowed_services("myproj", fraises)
        assert "myproj-nightly-backup.service" not in services
        assert "myproj-nightly-backup.timer" not in services

    def test_unit_installer_helper_units_rendered_when_scheduled_fraise_present(
        self, tmp_path
    ):
        """02 Phase 5 cycle 5.1 — type:scheduled fraise → unit-installer helper rendered."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  alerter:
    type: scheduled
    environments:
      production:
        app_path: /var/www/api
        jobs:
          poll:
            systemd_service: alerter-poll.service
            systemd_timer: alerter-poll.timer
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        systemd_dir = tmp_path / "output" / "systemd"
        socket_unit = systemd_dir / "fraisier-myproj-production-unit-installer.socket"
        service_unit = systemd_dir / "fraisier-myproj-production-unit-installer.service"
        assert socket_unit.exists()
        assert service_unit.exists()

    def test_unit_installer_helper_execstart_carries_deploy_user_and_allow(
        self, tmp_path
    ):
        """02 Phase 5 cycle 5.2 — ExecStart has --deploy-user, --project, --env, --allow."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
fraises:
  alerter:
    type: scheduled
    environments:
      production:
        app_path: /var/www/api
        jobs:
          poll:
            systemd_service: alerter-poll.service
            systemd_timer: alerter-poll.timer
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        service_unit = (
            tmp_path
            / "output"
            / "systemd"
            / "fraisier-myproj-production-unit-installer.service"
        )
        content = service_unit.read_text()
        execstart = next(
            line for line in content.splitlines() if line.startswith("ExecStart=")
        )
        assert "--deploy-user deployer" in execstart
        assert "--project myproj" in execstart
        assert "--env production" in execstart
        assert "--allow /var/www/api/scripts/systemd/:/etc/systemd/system/" in execstart

    def test_unit_installer_helper_not_rendered_without_scheduled_fraise(
        self, tmp_path
    ):
        """02 Phase 5 cycle 5.3 — no type:scheduled → no helper units (no dead units)."""
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

        systemd_dir = tmp_path / "output" / "systemd"
        assert not any(systemd_dir.glob("*unit-installer*"))

    def test_collect_allowed_services_omits_synthesised_entry_for_scheduled(self):
        """02 Phase 5 cycle 5.4 (folds 06) — no <project>_<scheduled>_<env>.service."""
        from fraisier.scaffold.renderer import _collect_allowed_services

        fraises = [
            {
                "name": "alerter",
                "type": "scheduled",
                "environments": {
                    "production": {
                        "app_path": "/var/www/api",
                        "jobs": {
                            "poll": {
                                "systemd_service": "alerter-poll.service",
                                "systemd_timer": "alerter-poll.timer",
                            }
                        },
                    }
                },
            }
        ]
        services = _collect_allowed_services("myproj", fraises)
        # Synthesised entry must NOT appear (#240 06).
        assert "myproj_alerter_production.service" not in services
        # The real per-job entries are still present.
        assert "alerter-poll.service" in services
        assert "alerter-poll.timer" in services

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

    def test_pg_wrapper_never_generated(self, tmp_path):
        """pg-wrapper.sh is never generated: admin DB ops use admin_url directly."""
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
          admin_url: postgresql:///postgres?host=/var/run/postgresql
      staging:
        app_path: /var/www/staging
        database:
          name: myapp_staging
          strategy: restore_migrate
          admin_url: postgresql:///postgres?host=/var/run/postgresql
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
        files = renderer.render()
        assert "pg-wrapper.sh" not in files
        assert not (tmp_path / "output" / "pg-wrapper.sh").exists()

    def test_sudoers_has_no_pg_wrapper_rule(self, tmp_path):
        """Sudoers no longer grants sudo-to-postgres via pg-wrapper: admin_url-only."""
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
          admin_url: postgresql:///postgres?host=/var/run/postgresql
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        # No pg-wrapper / pgadmin rule of any kind
        assert "pgadmin-myproj" not in content
        assert "pg-wrapper" not in content
        assert "(postgres)" not in content
        # Raw pg commands should also be absent
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
        # No trailing wildcard — sudo requires ` *` to match at least one further
        # argument, and the deploy path appends none (#294).
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

    def test_sudoers_uses_absolute_paths(self, tmp_path):
        """Sudoers entries use absolute paths for commands (#49)."""
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
          user: myapp
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        # Should use absolute path for uv
        assert "/usr/local/bin/uv sync --frozen" in content
        # Should not have relative path
        assert "NOPASSWD: uv" not in content

    def test_sudoers_deduplicates_identical_rules(self, tmp_path):
        """Identical sudoers rules are deduplicated across environments (#49)."""
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
      development:
        app_path: /var/www/dev
        install:
          command: [uv, sync, --frozen]
          user: myapp
      staging:
        app_path: /var/www/staging
        install:
          command: [uv, sync, --frozen]
          user: myapp
      production:
        app_path: /var/www/prod
        install:
          command: [uv, sync, --frozen]
          user: myapp
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        # The identical rule should appear only once
        count = content.count("uv sync --frozen")
        # Should appear once, not 3 times (one per environment)
        assert count == 1

    def test_sudoers_lists_affected_environments(self, tmp_path):
        """Sudoers rules include comments listing affected environments (#49)."""
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
      development:
        app_path: /var/www/dev
        install:
          command: [uv, sync, --frozen]
          user: myapp
      staging:
        app_path: /var/www/staging
        install:
          command: [uv, sync, --frozen]
          user: myapp
      production:
        app_path: /var/www/prod
        install:
          command: [uv, sync, --frozen]
          user: myapp
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "sudoers").read_text()
        # Should have comment listing all environments
        assert "Environments:" in content
        assert "development" in content
        assert "staging" in content
        assert "production" in content

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

    def test_install_sh_does_not_install_pg_wrapper(self, tmp_path):
        """install.sh no longer copies pg-wrapper.sh into /usr/local/libexec."""
        config = _make_full_config(tmp_path)
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "install.sh").read_text()
        assert "pg-wrapper" not in content
        assert "PG_WRAPPER_SRC" not in content
        assert "pgadmin-" not in content

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

    def test_install_sh_rebakes_install_helper_allowlist(self, tmp_path):
        """install.sh re-bakes a changed install.command's allowlist (#279).

        The running install-helper .service carries the old allowed_command in
        its argv, and `enable --now` is a no-op on a running unit — so install.sh
        must STOP the service and RESTART the socket, via _run_strict so a failed
        re-bake surfaces (and the deploy's post-pull gate aborts) instead of
        being swallowed by `_run … || true`.
        """
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
        install:
          command: [uv, sync, --frozen]
          user: myapp
"""
        )
        config = FraisierConfig(p)
        ScaffoldRenderer(config).render()

        content = (tmp_path / "output" / "install.sh").read_text()
        sock = "fraisier-tp-my_api-production-install-helper.socket"
        svc = "fraisier-tp-my_api-production-install-helper.service"

        # A fatal (non-swallowing) runner exists and gates the re-bake.
        assert "_run_strict()" in content
        # Copying the NEW unit files is fatal too — the load-bearing step: a
        # swallowed cp would re-exec the stale unit behind a green re-bake.
        assert "_run_strict sudo cp" in content
        assert f"/etc/systemd/system/{svc}" in content
        # ...and specifically the service copy is not swallowed by _run.
        assert f'_run sudo cp "${{SCAFFOLD_DIR}}/systemd/{svc}"' not in content
        # The stale-argv service is stopped and the socket restarted (fatal).
        assert f"_run_strict sudo systemctl stop {svc}" in content
        assert f"_run_strict sudo systemctl restart {sock}" in content
        # The old no-op form for the install-helper socket is gone.
        assert f"enable --now {sock}" not in content

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
        # Must use project-prefixed service names
        assert "myproj_my_api_production.service" in content
        # Must include socket/service units derived from fraise name + env key
        assert "fraisier-my_api-production.socket" in content
        assert "fraisier-my_api-production@.service" in content

    def test_webhook_service_never_sets_pg_wrapper_env(self, tmp_path):
        """Webhook service no longer injects FRAISIER_PG_WRAPPER (admin_url-only)."""
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
          admin_url: postgresql:///postgres?host=/var/run/postgresql
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

        content = (tmp_path / "output" / "fraisier-myproj-webhook.service").read_text()
        assert "FRAISIER_PG_WRAPPER" not in content
        assert "pgadmin-myproj" not in content

    def test_sudoers_ends_with_exactly_one_trailing_newline(self, tmp_path):
        """Generated sudoers file ends with one \\n, not two.

        Regression test for #161: the previous template emitted a blank line
        after every rule including the last, leaving the file ending in
        ``\\n\\n``. pre-commit's end-of-file-fixer flagged it on every run.
        """
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = _make_full_config(tmp_path)
        renderer = ScaffoldRenderer(config)

        rules = [
            {
                "from_user": "deployer",
                "as_user": "root",
                "cmd": "/usr/bin/apt-get install",
                "environments": ["development", "production"],
                "description": "Dependency install",
            },
            {
                "from_user": "deployer",
                "as_user": "postgres",
                "cmd": "/usr/bin/createdb",
                "environments": ["development"],
                "description": "Dependency install",
            },
        ]

        template = renderer.env.get_template("core/sudoers.j2")
        content = template.render(
            project_name="testproj",
            sudoers_rules=rules,
        )

        assert content.endswith("\n"), "sudoers must end with a newline"
        assert not content.endswith("\n\n"), (
            "sudoers must not end with a blank line (pre-commit end-of-file-fixer flags it)"
        )

        body = content.split("# Dependency install rules (deduplicated)\n", 1)[1]
        rule_blocks = [b for b in body.split("/usr/bin/") if b]
        assert len(rule_blocks) >= 2, "expected both rules to render"
        between = body.split("/usr/bin/apt-get install", 1)[1]
        between = between.split("# Dependency install", 1)[0]
        assert "\n\n" in between, (
            "blank line between rules should be preserved for human-readability"
        )

    def test_sudoers_single_rule_no_trailing_blank(self, tmp_path):
        """Single-rule sudoers also ends cleanly with one \\n."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = _make_full_config(tmp_path)
        renderer = ScaffoldRenderer(config)

        rules = [
            {
                "from_user": "deployer",
                "as_user": "root",
                "cmd": "/usr/bin/apt-get install",
                "environments": ["production"],
                "description": "Dependency install",
            },
        ]

        template = renderer.env.get_template("core/sudoers.j2")
        content = template.render(
            project_name="testproj",
            sudoers_rules=rules,
        )

        assert content.endswith("\n")
        assert not content.endswith("\n\n")


class TestWebhookServerFiltering:
    """Webhook service filters ReadWritePaths by server (#62)."""

    def test_webhook_includes_only_local_server_paths(self, tmp_path):
        """Webhook service only includes ReadWritePaths for environments on the host.

        Deliberate contract change (#325): this used to read the *unslugged*
        ``fraisier-myproj-webhook.service``, because a ``--server`` render
        wrote the host-agnostic name with host-filtered content. That pairing
        is the whole bug — the file's content depended on who rendered it
        last, so the deploy path's unfiltered regeneration replaced it (or
        failed to, leaving a stale one) and the installer copied whatever
        survived. The filter it pins is unchanged and still correct; only the
        filename moved, and it now reads the slugged file the host installs.
        Invariant (M) — mode is a function of the config, never of
        ``--server`` — is what makes the unslugged name unreachable here.
        """
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
environments:
  development:
    server: server-1
  production:
    server: server-2
fraises:
  my_api:
    type: api
    environments:
      development:
        app_path: /var/www/dev
      production:
        app_path: /var/www/prod
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config, server="server-1")
        renderer.render()

        content = (
            tmp_path / "output" / "fraisier-myproj-webhook-server-1.service"
        ).read_text()
        assert "ReadWritePaths=/var/www/dev" in content
        assert "ReadWritePaths=/var/www/prod" not in content
        assert not (tmp_path / "output" / "fraisier-myproj-webhook.service").exists()

    def test_webhook_without_server_generates_per_server_files(self, tmp_path):
        """Without --server and environments.server set, one file per server is made."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
environments:
  development:
    server: server-1
  production:
    server: server-2
fraises:
  my_api:
    type: api
    environments:
      development:
        app_path: /var/www/dev
      production:
        app_path: /var/www/prod
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)  # No server specified
        renderer.render()

        dev_content = (
            tmp_path / "output" / "fraisier-myproj-webhook-server-1.service"
        ).read_text()
        prod_content = (
            tmp_path / "output" / "fraisier-myproj-webhook-server-2.service"
        ).read_text()
        assert "ReadWritePaths=/var/www/dev" in dev_content
        assert "ReadWritePaths=/var/www/prod" not in dev_content
        assert "ReadWritePaths=/var/www/prod" in prod_content
        assert "ReadWritePaths=/var/www/dev" not in prod_content

    def test_webhook_without_server_no_server_config_single_file(self, tmp_path):
        """Without --server and no environments.server, one webhook file is made."""
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
      production:
        app_path: /var/www/prod
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        files = renderer.render(dry_run=True)

        assert "fraisier-myproj-webhook.service" in files
        assert "fraisier-myproj-webhook-server-1.service" not in files

    def test_auto_per_server_dry_run_returns_per_server_filenames(self, tmp_path):
        """dry_run=True includes per-server webhook filenames in the returned list."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
environments:
  development:
    server: server-1
  production:
    server: server-2
fraises:
  my_api:
    type: api
    environments:
      development:
        app_path: /var/www/dev
      production:
        app_path: /var/www/prod
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        files = renderer.render(dry_run=True)

        assert "fraisier-myproj-webhook-server-1.service" in files
        assert "fraisier-myproj-webhook-server-2.service" in files
        assert "fraisier-myproj-webhook.service" not in files

    def test_webhook_server_with_no_matching_environments_is_an_error(self, tmp_path):
        """An unknown --server is rejected, naming it and the servers that exist.

        Deliberate contract change (#325). The old assertion — that
        ``--server server-3`` renders a unit with the fraisier state dirs and
        no application paths, silently and with exit 0 — *was* the bug it
        pinned. A typo'd or stale ``--server`` produced a valid-looking,
        installable unit that then failed every deploy on
        ``Read-only file system``, because ``ProtectSystem=strict`` denies
        every path the render quietly dropped.

        A regeneration must never silently narrow: a render that cannot
        produce a correct unit has to abort before the install step rather
        than emit a narrower one. ``_regenerate_scaffold`` already turns a
        non-zero scaffold exit into a DeploymentError, so this aborts the
        deploy at the right range.
        """
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {tmp_path / "output"}
environments:
  development:
    server: server-1
  production:
    server: server-2
fraises:
  my_api:
    type: api
    environments:
      development:
        app_path: /var/www/dev
      production:
        app_path: /var/www/prod
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config, server="server-3")  # Non-existent server

        with pytest.raises(ValidationError) as exc:
            renderer.render()

        message = str(exc.value)
        assert "server-3" in message
        assert "server-1" in message and "server-2" in message


class TestWebhookNaming:
    """Webhook service uses project-specific names (#63)."""

    def _make_config(self, tmp_path, project_name="myproj"):
        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: {project_name}
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
        from fraisier.config import FraisierConfig

        return FraisierConfig(p)

    def test_webhook_filename_uses_project_name(self, tmp_path):
        """render(dry_run=True) returns fraisier-{project}-webhook.service filename."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(tmp_path, "myproj")
        renderer = ScaffoldRenderer(config)
        files = renderer.render(dry_run=True)

        assert "fraisier-myproj-webhook.service" in files
        assert "fraisier-webhook.service" not in files

    def test_webhook_filename_reflects_project_name(self, tmp_path):
        """Different project names produce different service file names."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(tmp_path, "acme")
        renderer = ScaffoldRenderer(config)
        files = renderer.render(dry_run=True)

        assert "fraisier-acme-webhook.service" in files
        assert "fraisier-myproj-webhook.service" not in files

    def test_webhook_environment_file_uses_project_name(self, tmp_path):
        """EnvironmentFile directive uses /etc/fraisier/{project}.webhook.env."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(tmp_path, "myproj")
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "fraisier-myproj-webhook.service").read_text()
        assert "EnvironmentFile=/etc/fraisier/myproj.webhook.env" in content
        assert "EnvironmentFile=/etc/fraisier/webhook.env" not in content


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
          admin_url: postgresql:///postgres?host=/var/run/postgresql
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
          admin_url: postgresql:///postgres?host=/var/run/postgresql
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
        # The PostgreSQL *logging* section only appears when has_database is true
        assert "PostgreSQL logging configs" not in content


class TestWildcardCorsOrigins:
    """CORS wildcard processing — issue #147."""

    def test_wildcard_produces_anchored_regex_with_no_dot_class(self):
        """Wildcard CORS origin produces ^…[^.]+…$ nginx regex (issue #147)."""
        from fraisier.config.schema import _process_cors_origin

        result = _process_cors_origin(
            {"pattern": "https://*.example.com", "type": "wildcard"}
        )
        assert result == "^https://[^.]+\\.example\\.com$"

    def test_wildcard_no_protocol_produces_anchored_regex(self):
        """Bare wildcard *.example.io also anchored (issue #147)."""
        from fraisier.config.schema import _process_cors_origin

        result = _process_cors_origin({"pattern": "*.example.io", "type": "wildcard"})
        assert result == "^[^.]+\\.example\\.io$"

    def test_wildcard_renders_in_nginx_map(self, tmp_path):
        """gateway.conf map block contains anchored pattern for wildcard origin (issue #147)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: tp
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 2
scaffold:
  output_dir: {tmp_path / "output"}
  nginx:
    cors_origins:
      - {{pattern: "https://*.example.com", type: wildcard}}
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "nginx" / "gateway.conf").read_text()
        assert "^https://[^.]+\\.example\\.com$" in content


class TestInstallShScaffoldDir:
    """install.sh SCAFFOLD_DIR defaults to script directory — issue #144."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_install_sh_defaults_scaffold_dir_to_script_directory(self, tmp_path):
        """install.sh must default SCAFFOLD_DIR to its own directory, not PROJECT_DIR (issue #144)."""
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
        worker_count: 2
""",
        )
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "install.sh").read_text()
        # Must default to the script's own directory, not PROJECT_DIR/scripts/generated
        assert 'dirname "$(realpath "$0")"' in content
        assert "${PROJECT_DIR}/scripts/generated" not in content


class TestInstallShRateLimitConflict:
    """install.sh removes legacy rate_limit.conf before nginx reload — issue #145."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_install_sh_removes_legacy_rate_limit_conf(self, tmp_path):
        """install.sh detects and removes /etc/nginx/conf.d/rate_limit.conf (issue #145)."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(
            tmp_path,
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
  nginx:
    rate_limit: "10r/s"
fraises:
  my_api:
    type: api
    environments:
      production:
        worker_count: 2
""",
        )
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "install.sh").read_text()
        assert "rate_limit.conf" in content
        assert "limit_req_zone" in content
        assert "rm" in content

    def test_install_sh_nginx_reload_not_masked(self, tmp_path):
        """nginx reload in install.sh must not be swallowed by _run || true (issue #145)."""
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
        worker_count: 2
""",
        )
        renderer = ScaffoldRenderer(config)
        renderer.render()

        content = (tmp_path / "output" / "install.sh").read_text()
        # nginx reload must be direct (not inside _run) so failures surface
        assert "sudo nginx -t && sudo systemctl reload nginx" in content
        # The reload line itself must not have || true appended
        reload_line = next(
            (l for l in content.splitlines() if "systemctl reload nginx" in l), ""
        )
        assert "|| true" not in reload_line


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
          cors_origins:
            - {{pattern: "https://app.dev.example.com", type: literal}}
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
          cors_origins:
            - {{pattern: "https://app.example.com", type: literal}}
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
        assert "nginx/api.dev.example.com.conf" in files
        assert "nginx/api.example.com.conf" in files

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
            tmp_path / "output" / "nginx" / "api.example.com.conf"
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

    def test_scaffold_output_dir_override(self, tmp_path):
        """--output-dir renders into the given dir, not scaffold.output_dir (#283)."""
        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCAFFOLD_YAML.format(output=str(tmp_path / "output")))
        override = tmp_path / "state"

        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", str(cfg), "scaffold", "--output-dir", str(override)]
        )
        assert result.exit_code == 0
        assert (override / "install.sh").exists()
        assert not (tmp_path / "output").exists()

    def test_scaffold_install_output_dir_override_looked_up(self, tmp_path):
        """scaffold-install --output-dir reads install.sh from that dir (#283)."""
        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCAFFOLD_YAML.format(output=str(tmp_path / "output")))
        override = tmp_path / "state"
        override.mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                str(cfg),
                "scaffold-install",
                "--output-dir",
                str(override),
                "--yes",
            ],
        )
        # No install.sh in the override dir → the error must name that path,
        # proving --output-dir (not scaffold.output_dir) was consulted.
        assert result.exit_code != 0
        assert str(override / "install.sh") in result.output

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

    def test_scaffold_install_command_missing_install_script(self, tmp_path):
        """scaffold-install fails if install.sh doesn't exist."""
        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises: {{}}
"""
        )

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold-install"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_scaffold_install_command_unreadable_install_script(
        self, tmp_path, monkeypatch
    ):
        """scaffold-install reports a friendly error when install.sh is unreadable.

        Regression for #222: `Path.exists()` propagates `PermissionError` when a
        parent directory of the install script is not traversable. The CLI must
        treat that the same as "not found" and exit cleanly instead of crashing
        with an unhandled traceback.
        """
        from pathlib import Path

        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises: {{}}
"""
        )

        real_exists = Path.exists
        unreadable = tmp_path / "output" / "install.sh"

        def fake_exists(self, *args, **kwargs):
            if self == unreadable:
                raise PermissionError(13, "Permission denied", str(self))
            return real_exists(self, *args, **kwargs)

        monkeypatch.setattr(Path, "exists", fake_exists)

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold-install"])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output
        assert "not found" in result.output.lower()

    def test_scaffold_install_command_is_file_permission_error(
        self, tmp_path, monkeypatch
    ):
        """scaffold-install handles PermissionError from is_file() cleanly (#222)."""
        from pathlib import Path

        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises: {{}}
"""
        )
        install_script = tmp_path / "output" / "install.sh"
        install_script.parent.mkdir(parents=True)
        install_script.write_text("#!/bin/sh\n")

        real_is_file = Path.is_file

        def fake_is_file(self, *args, **kwargs):
            if self == install_script:
                raise PermissionError(13, "Permission denied", str(self))
            return real_is_file(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_file", fake_is_file)

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold-install"])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output

    def test_scaffold_install_command_chmod_permission_error(
        self, tmp_path, monkeypatch
    ):
        """scaffold-install handles PermissionError from chmod() cleanly (#222)."""
        from pathlib import Path

        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises: {{}}
"""
        )
        install_script = tmp_path / "output" / "install.sh"
        install_script.parent.mkdir(parents=True)
        install_script.write_text("#!/bin/sh\n")
        install_script.chmod(0o644)  # not executable

        real_chmod = Path.chmod

        def fake_chmod(self, *args, **kwargs):
            if self == install_script:
                raise PermissionError(1, "Operation not permitted", str(self))
            return real_chmod(self, *args, **kwargs)

        monkeypatch.setattr(Path, "chmod", fake_chmod)

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold-install", "--yes"])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output
        assert "executable" in result.output.lower()

    def test_scaffold_install_failure_message_includes_exit_code(
        self, tmp_path, monkeypatch
    ):
        """scaffold-install --dry-run failure surfaces exit code + log hint (#225).

        Regression for #225: when install.sh exits non-zero in dry-run/validate
        mode, the wrapper's failure message must include the actual exit code
        and a copy-pasteable command (with --verbose + tee) that produces a
        persistent log. Replaces the previous "Review the output above" hint
        that was useless when no output existed.
        """
        from click.testing import CliRunner

        from fraisier.cli import main
        from fraisier.cli import scaffold as scaffold_mod

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises: {{}}
"""
        )
        install_script = tmp_path / "output" / "install.sh"
        install_script.parent.mkdir(parents=True)
        install_script.write_text("#!/bin/sh\nexit 42\n")
        install_script.chmod(0o755)

        monkeypatch.setattr(scaffold_mod, "_run_script", lambda _cmd: 42)

        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", str(cfg), "scaffold-install", "--dry-run", "--yes"]
        )
        assert result.exit_code == 42
        assert "42" in result.output
        assert "install.sh" in result.output
        assert "tee" in result.output

    def test_scaffold_install_install_failure_message_includes_exit_code(
        self, tmp_path, monkeypatch
    ):
        """scaffold-install (non-preview) failure surfaces exit code + hint (#225).

        Parallel regression to the dry-run path: when install.sh exits non-zero
        outside of preview/validate, the message must include the exit code and
        the verbose-log rerun hint.
        """
        from click.testing import CliRunner

        from fraisier.cli import main
        from fraisier.cli import scaffold as scaffold_mod

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            f"""
name: tp
scaffold:
  output_dir: {tmp_path / "output"}
fraises: {{}}
"""
        )
        install_script = tmp_path / "output" / "install.sh"
        install_script.parent.mkdir(parents=True)
        install_script.write_text("#!/bin/sh\nexit 7\n")
        install_script.chmod(0o755)

        monkeypatch.setattr(scaffold_mod, "_run_script", lambda _cmd: 7)

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold-install", "--yes"])
        assert result.exit_code == 7
        assert "7" in result.output
        assert "install.sh" in result.output
        assert "tee" in result.output
        assert "Installation failed" in result.output

    def test_scaffold_then_install_workflow(self, tmp_path):
        """Complete workflow: scaffold generates files, scaffold-install installs."""
        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCAFFOLD_YAML.format(output=str(tmp_path / "output")))

        runner = CliRunner()

        # Step 1: Generate scaffold files
        result = runner.invoke(main, ["-c", str(cfg), "scaffold"])
        assert result.exit_code == 0
        install_script = tmp_path / "output" / "install.sh"
        assert install_script.exists()

        # Step 2: Check that install.sh mentions next steps
        assert "scaffold-install" in result.output.lower()

        # Step 3: Verify install.sh has dry-run support
        assert install_script.exists()
        content = install_script.read_text()
        assert "--dry-run" in content
        assert "--validate-only" in content

    def test_scaffold_install_help(self, tmp_path):
        """scaffold-install --help shows usage information."""
        from click.testing import CliRunner

        from fraisier.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["scaffold-install", "--help"])
        assert result.exit_code == 0
        assert "preview" in result.output.lower()
        assert "validate" in result.output.lower()
        assert "--dry-run" in result.output
        assert "--validate-only" in result.output

    def test_scaffold_generates_install_suggestion(self, tmp_path):
        """After scaffold, output suggests running scaffold-install."""
        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(_SCAFFOLD_YAML.format(output=str(tmp_path / "output")))

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold"])
        assert result.exit_code == 0
        # Should mention the next command
        assert "scaffold-install" in result.output
        assert "--dry-run" in result.output
        assert "--yes" in result.output

    # --- #224: sudoers diff-and-warn before overwrite -----------------------

    def _setup_sudoers_install(self, tmp_path, *, source_sudoers: str):
        """Build a minimal install.sh + rendered sudoers for the #224 tests."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            f"""
name: myproj
scaffold:
  output_dir: {tmp_path / "output"}
fraises: {{}}
"""
        )
        output_dir = tmp_path / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        install_script = output_dir / "install.sh"
        install_script.write_text("#!/bin/sh\nexit 0\n")
        install_script.chmod(0o755)
        sudoers_src = output_dir / "sudoers"
        sudoers_src.write_text(source_sudoers)
        return cfg

    def test_sudoers_diff_warns_on_removed_rules(self, tmp_path, monkeypatch):
        """A rule on disk but not in the rendered fragment surfaces as 'removed' (#224)."""
        from click.testing import CliRunner

        from fraisier.cli import main
        from fraisier.cli import scaffold as scaffold_mod

        cfg = self._setup_sudoers_install(
            tmp_path,
            source_sudoers="user1 ALL=(root) NOPASSWD: /usr/bin/foo\n",
        )

        # Currently on disk: an extra rule that's about to disappear.
        monkeypatch.setattr(
            scaffold_mod,
            "_read_current_sudoers",
            lambda _name: (
                "user1 ALL=(root) NOPASSWD: /usr/bin/foo\n"
                "admin ALL=(root) NOPASSWD: /usr/bin/baz\n",
                "ok",
            ),
        )
        monkeypatch.setattr(scaffold_mod, "_run_script", lambda _cmd: 0)

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold-install", "--yes"])
        assert result.exit_code == 0
        assert "would be removed" in result.output.lower()
        assert "admin ALL=(root) NOPASSWD: /usr/bin/baz" in result.output

    def test_sudoers_diff_silent_when_fresh_install(self, tmp_path, monkeypatch):
        """No target file on disk → no diff section, no warning (#224)."""
        from click.testing import CliRunner

        from fraisier.cli import main
        from fraisier.cli import scaffold as scaffold_mod

        cfg = self._setup_sudoers_install(
            tmp_path,
            source_sudoers="user1 ALL=(root) NOPASSWD: /usr/bin/foo\n",
        )
        monkeypatch.setattr(
            scaffold_mod,
            "_read_current_sudoers",
            lambda _name: (None, "missing"),
        )
        monkeypatch.setattr(scaffold_mod, "_run_script", lambda _cmd: 0)

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold-install", "--yes"])
        assert result.exit_code == 0
        assert "would be removed" not in result.output.lower()

    def test_sudoers_diff_silent_when_identical(self, tmp_path, monkeypatch):
        """Identical current and new → no diff section (#224)."""
        from click.testing import CliRunner

        from fraisier.cli import main
        from fraisier.cli import scaffold as scaffold_mod

        rule = "user1 ALL=(root) NOPASSWD: /usr/bin/foo\n"
        cfg = self._setup_sudoers_install(tmp_path, source_sudoers=rule)
        monkeypatch.setattr(
            scaffold_mod, "_read_current_sudoers", lambda _name: (rule, "ok")
        )
        monkeypatch.setattr(scaffold_mod, "_run_script", lambda _cmd: 0)

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold-install", "--yes"])
        assert result.exit_code == 0
        assert "would be removed" not in result.output.lower()

    def test_sudoers_diff_unreadable_non_strict_warns_and_proceeds(
        self, tmp_path, monkeypatch
    ):
        """Unreadable sudoers + no --strict-sudoers → note + skip diff + proceed."""
        from click.testing import CliRunner

        from fraisier.cli import main
        from fraisier.cli import scaffold as scaffold_mod

        cfg = self._setup_sudoers_install(
            tmp_path,
            source_sudoers="user1 ALL=(root) NOPASSWD: /usr/bin/foo\n",
        )
        monkeypatch.setattr(
            scaffold_mod,
            "_read_current_sudoers",
            lambda _name: (None, "unreadable"),
        )
        monkeypatch.setattr(scaffold_mod, "_run_script", lambda _cmd: 0)

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(cfg), "scaffold-install", "--yes"])
        assert result.exit_code == 0
        assert "could not read" in result.output.lower()

    def test_strict_sudoers_aborts_with_exit_code_3(self, tmp_path, monkeypatch):
        """--strict-sudoers + diff → exit 3, do not run install (#224)."""
        from click.testing import CliRunner

        from fraisier.cli import main
        from fraisier.cli import scaffold as scaffold_mod

        cfg = self._setup_sudoers_install(
            tmp_path,
            source_sudoers="user1 ALL=(root) NOPASSWD: /usr/bin/foo\n",
        )
        monkeypatch.setattr(
            scaffold_mod,
            "_read_current_sudoers",
            lambda _name: (
                "user1 ALL=(root) NOPASSWD: /usr/bin/foo\n"
                "admin ALL=(root) NOPASSWD: /usr/bin/baz\n",
                "ok",
            ),
        )

        ran: list[list[str]] = []

        def fake_run(cmd):
            ran.append(cmd)
            return 0

        monkeypatch.setattr(scaffold_mod, "_run_script", fake_run)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["-c", str(cfg), "scaffold-install", "--yes", "--strict-sudoers"],
        )
        assert result.exit_code == 3
        assert "strict-sudoers" in result.output.lower()
        assert ran == []  # install must not have been invoked

    def test_strict_sudoers_aborts_on_unreadable_sudoers(self, tmp_path, monkeypatch):
        """--strict-sudoers + unreadable target → exit 3 (couldn't verify, #224)."""
        from click.testing import CliRunner

        from fraisier.cli import main
        from fraisier.cli import scaffold as scaffold_mod

        cfg = self._setup_sudoers_install(
            tmp_path,
            source_sudoers="user1 ALL=(root) NOPASSWD: /usr/bin/foo\n",
        )
        monkeypatch.setattr(
            scaffold_mod,
            "_read_current_sudoers",
            lambda _name: (None, "unreadable"),
        )
        monkeypatch.setattr(scaffold_mod, "_run_script", lambda _cmd: 0)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["-c", str(cfg), "scaffold-install", "--yes", "--strict-sudoers"],
        )
        assert result.exit_code == 3
        assert "could not read" in result.output.lower()

    def test_scaffold_install_help_includes_strict_sudoers(self, tmp_path):
        """scaffold-install --help lists the new --strict-sudoers flag (#224)."""
        from click.testing import CliRunner

        from fraisier.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["scaffold-install", "--help"])
        assert result.exit_code == 0
        assert "--strict-sudoers" in result.output


class TestDeploySocketServiceUnits:
    """Regression tests for issue #72 — socket/service unit correctness."""

    def _render(self, tmp_path):
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
scaffold:
  output_dir: {tmp_path / "output"}
  deploy_user: myproj_deploy
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()
        return tmp_path / "output"

    def test_socket_uses_accept_yes(self, tmp_path):
        """Accept=yes is required — deploy-daemon reads from stdin (pre-connected
        socket).

        Accept=yes requires a template service unit named deploy@.service so
        systemd can spawn one instance per connection (issue #72, Bug 1).
        """
        out = self._render(tmp_path)
        socket_path = out / "systemd" / "fraisier-api-production.socket"
        socket = socket_path.read_text()
        assert "Accept=yes" in socket

    def test_socket_does_not_use_accept_no(self, tmp_path):
        out = self._render(tmp_path)
        socket_path = out / "systemd" / "fraisier-api-production.socket"
        socket = socket_path.read_text()
        assert "Accept=no" not in socket

    def test_service_has_no_standard_output_format(self, tmp_path):
        """StandardOutputFormat=json must not appear in the service unit.

        This key was introduced in systemd 255; Debian 12 / Ubuntu 22.04
        ship systemd 252-253 and log a parse warning, breaking deployments
        (issue #72, Bug 2).
        """
        out = self._render(tmp_path)
        service = (out / "systemd" / "fraisier-api-production@.service").read_text()
        assert "StandardOutputFormat" not in service

    def test_service_still_has_journal_output(self, tmp_path):
        """StandardOutput=journal must remain after removing the format key."""
        out = self._render(tmp_path)
        service = (out / "systemd" / "fraisier-api-production@.service").read_text()
        assert "StandardOutput=journal" in service
        assert "StandardError=journal" in service

    def test_service_has_no_protect_home(self, tmp_path):
        """ProtectHome must not appear in the deploy service unit.

        fraisier is installed via 'uv tool install' as a symlink chain
        entirely within /home (~/.local/bin → ~/.local/share/uv/tools/...).
        Any ProtectHome value — including read-only — causes systemd 252 to
        fail to exec the binary with ENOENT (issue #72, Bug 3).
        """
        out = self._render(tmp_path)
        service = (out / "systemd" / "fraisier-api-production@.service").read_text()
        assert "ProtectHome=" not in service

    def test_socket_listens_on_expected_path(self, tmp_path):
        out = self._render(tmp_path)
        socket_path = out / "systemd" / "fraisier-api-production.socket"
        socket = socket_path.read_text()
        assert (
            "ListenStream=/run/fraisier/fraisier-api-production/deploy.sock" in socket
        )

    def test_service_requires_correct_socket_unit(self, tmp_path):
        """Service unit Requires= must name the socket unit derived from env name."""
        out = self._render(tmp_path)
        service = (out / "systemd" / "fraisier-api-production@.service").read_text()
        assert "Requires=fraisier-api-production.socket" in service
        assert "After=fraisier-api-production.socket" in service

    def test_service_exec_uses_fraise_name(self, tmp_path):
        """deploy-daemon --project must receive the fraise name, not the project name.

        The socket is per-fraise (fraisier-api-production.socket is for fraise 'api'),
        so the daemon security gate must match the fraise name. trigger-deploy also
        sends {"project": "<fraise_name>", ...} so the mismatch check passes.
        Using the top-level project name would cause the daemon's fraise lookup to
        fail since config.get_fraise_environment() expects a fraise name.
        """
        out = self._render(tmp_path)
        service = (out / "systemd" / "fraisier-api-production@.service").read_text()
        assert "--project=api" in service
        assert "--project=myproj" not in service

    def test_socket_filenames_use_fraise_and_env_name(self, tmp_path):
        """Unit filenames include both fraise name and env key for uniqueness."""
        out = self._render(tmp_path)
        sdir = out / "systemd"
        assert (sdir / "fraisier-api-production.socket").exists()
        assert (sdir / "fraisier-api-production@.service").exists()
        assert not (sdir / "fraisier-myproj-api-production-deploy.socket").exists()
        assert not (sdir / "fraisier-myproj-api-production-deploy@.service").exists()

    def test_service_sets_fraisier_config_default(self, tmp_path):
        """Service unit sets FRAISIER_CONFIG to the default system-wide path.

        The deploy daemon starts in a clean systemd environment with no config
        path set. Without this env var, it cannot locate fraises.yaml (issue #72,
        Bug 5).
        """
        out = self._render(tmp_path)
        service = (out / "systemd" / "fraisier-api-production@.service").read_text()
        assert "Environment=FRAISIER_CONFIG=/opt/fraisier/fraises.yaml" in service

    def test_service_config_path_is_configurable(self, tmp_path):
        """scaffold.config_path overrides the FRAISIER_CONFIG value in the unit."""
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
scaffold:
  output_dir: {tmp_path / "output"}
  deploy_user: myproj_deploy
  config_path: /etc/myapp/fraises.yaml
"""
        )
        config = FraisierConfig(p)
        ScaffoldRenderer(config).render()
        svc_path = tmp_path / "output" / "systemd" / "fraisier-api-production@.service"
        service = svc_path.read_text()
        assert "Environment=FRAISIER_CONFIG=/etc/myapp/fraises.yaml" in service
        assert "/opt/fraisier/fraises.yaml" not in service

    def test_deploy_environment_file_omitted_by_default(self, tmp_path):
        """EnvironmentFile must not appear when deploy_environment_file is unset."""
        out = self._render(tmp_path)
        service = (out / "systemd" / "fraisier-api-production@.service").read_text()
        assert "EnvironmentFile" not in service

    def test_deploy_environment_file_rendered(self, tmp_path):
        """scaffold.deploy_environment_file renders EnvironmentFile directive."""
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
scaffold:
  output_dir: {tmp_path / "output"}
  deploy_user: myproj_deploy
  deploy_environment_file: /etc/fraisier/secrets.env
"""
        )
        config = FraisierConfig(p)
        ScaffoldRenderer(config).render()
        service = (
            tmp_path / "output" / "systemd" / "fraisier-api-production@.service"
        ).read_text()
        assert "EnvironmentFile=-/etc/fraisier/secrets.env" in service

    def test_deploy_service_has_readwrite_paths_for_git_repo_and_app_path(
        self, tmp_path
    ):
        """deploy service includes ReadWritePaths for git_repo and app_path.

        ProtectSystem=strict makes the filesystem read-only, which prevents
        the deploy daemon from running 'git fetch' (writes to the bare repo)
        and 'git checkout' (writes to the app worktree). ReadWritePaths
        exceptions are required for both paths (issue #115).
        """
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myproj
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api.myapp.io
        git_repo: /var/git/api.myapp.io.git
scaffold:
  output_dir: {tmp_path / "output"}
  deploy_user: myproj_deploy
"""
        )
        config = FraisierConfig(p)
        ScaffoldRenderer(config).render()
        service = (
            tmp_path / "output" / "systemd" / "fraisier-api-production@.service"
        ).read_text()
        assert "ReadWritePaths=/var/git/api.myapp.io.git" in service
        assert "ReadWritePaths=/var/www/api.myapp.io" in service
        assert "ReadWritePaths=/run/fraisier" in service

    def test_deploy_service_omits_readwrite_paths_when_not_configured(self, tmp_path):
        """ReadWritePaths for git_repo/app_path are omitted when not set."""
        out = self._render(tmp_path)  # fixture has app_path but no git_repo
        service = (out / "systemd" / "fraisier-api-production@.service").read_text()
        assert "ReadWritePaths=/var/www/prod" in service
        assert "ReadWritePaths=/run/fraisier" in service
        # git_repo not set, so no ReadWritePaths for it
        assert "git_repo" not in service

    def test_deploy_service_has_readwrite_paths_for_config_dir(self, tmp_path):
        """deploy service includes ReadWritePaths for the config file directory.

        ProtectSystem=strict makes /opt read-only. The deploy daemon's config
        sync step must be able to write the updated fraises.yaml to that directory
        (issue #115).
        """
        out = self._render(tmp_path)
        service = (out / "systemd" / "fraisier-api-production@.service").read_text()
        # Default config_path is /opt/fraisier/fraises.yaml → dir is /opt/fraisier
        assert "ReadWritePaths=/opt/fraisier" in service

    def test_deploy_service_has_git_ssh_command(self, tmp_path):
        """deploy service unit includes GIT_SSH_COMMAND to bypass known_hosts check.

        Bootstrap creates the deploy user's SSH keypair but does not populate
        ~/.ssh/known_hosts. Without StrictHostKeyChecking=accept-new the first
        git fetch fails with SSH exit 255 (issue #116). The compact -oKey=Value
        form is used so systemd parses it as one Environment= value (#152).
        """
        out = self._render(tmp_path)
        service = (out / "systemd" / "fraisier-api-production@.service").read_text()
        ssh_env = 'Environment="GIT_SSH_COMMAND=ssh -oStrictHostKeyChecking=accept-new"'
        assert ssh_env in service


class TestServiceNameOverride:
    """service.service_name overrides the generated systemd unit filename."""

    def _render(self, tmp_path, yaml_extra: str = ""):
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(
            f"""
name: myapp
fraises:
  api:
    type: api
    environments:
      dev:
        app_path: /var/www/dev
        service:
          service_name: api.dev.example.com
      production:
        app_path: /var/www/prod
scaffold:
  deploy_user: myapp_deploy
  output_dir: {tmp_path / "output"}
{yaml_extra}
"""
        )
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config)
        renderer.render()
        return tmp_path / "output"

    def test_service_file_uses_override_name(self, tmp_path):
        """Renderer writes the unit file using the overridden name."""
        out = self._render(tmp_path)
        assert (out / "systemd" / "api.dev.example.com.service").exists()
        assert not (out / "systemd" / "myapp_api_dev.service").exists()

    def test_default_name_used_when_no_override(self, tmp_path):
        """Environments without service_name still use the default pattern."""
        out = self._render(tmp_path)
        assert (out / "systemd" / "myapp_api_production.service").exists()

    def test_systemctl_wrapper_uses_override_name(self, tmp_path):
        """allowed_services list in the systemctl wrapper uses the override."""
        out = self._render(tmp_path)
        wrapper = (out / "systemctl-wrapper.sh").read_text()
        assert "api.dev.example.com.service" in wrapper
        assert "myapp_api_dev.service" not in wrapper

    def test_install_sh_uses_override_name(self, tmp_path):
        """install.sh cp command targets the overridden filename."""
        out = self._render(tmp_path)
        install = (out / "install.sh").read_text()
        assert "api.dev.example.com.service" in install
        assert "myapp_api_dev.service" not in install


class TestSystemdServiceField:
    """systemd_service at env top level overrides the generated unit filename."""

    def _render(self, tmp_path, yaml: str):
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(yaml)
        config = FraisierConfig(p)
        ScaffoldRenderer(config).render()
        return tmp_path / "output"

    def _base_yaml(self, tmp_path, dev_name: str) -> str:
        return f"""
name: myapp
fraises:
  api:
    type: api
    environments:
      dev:
        app_path: /var/www/dev
        systemd_service: {dev_name}
      production:
        app_path: /var/www/prod
scaffold:
  deploy_user: myapp_deploy
  output_dir: {tmp_path / "output"}
"""

    def test_systemd_service_with_suffix(self, tmp_path):
        """systemd_service with .service suffix produces the correct filename."""
        yaml = self._base_yaml(tmp_path, "api.dev.example.com.service")
        out = self._render(tmp_path, yaml)
        assert (out / "systemd" / "api.dev.example.com.service").exists()
        assert not (out / "systemd" / "myapp_api_dev.service").exists()

    def test_systemd_service_without_suffix(self, tmp_path):
        """systemd_service without .service suffix produces the correct filename."""
        out = self._render(tmp_path, self._base_yaml(tmp_path, "api.dev.example.com"))
        assert (out / "systemd" / "api.dev.example.com.service").exists()
        assert not (out / "systemd" / "myapp_api_dev.service").exists()

    def test_systemd_service_propagates_to_wrapper_and_install(self, tmp_path):
        """Override appears in systemctl-wrapper.sh allowed list and install.sh."""
        yaml = self._base_yaml(tmp_path, "api.dev.example.com.service")
        out = self._render(tmp_path, yaml)
        wrapper = (out / "systemctl-wrapper.sh").read_text()
        assert "api.dev.example.com.service" in wrapper
        assert "myapp_api_dev.service" not in wrapper
        install = (out / "install.sh").read_text()
        assert "api.dev.example.com.service" in install
        assert "myapp_api_dev.service" not in install

    def test_systemd_service_invalid_chars_raises_at_load_time(self, tmp_path):
        """Invalid systemd_service raises ValidationError at config load."""
        import pytest

        from fraisier.config import ValidationError
        from tests._eager_load import eager_load

        p = tmp_path / "fraises.yaml"
        p.write_text(f"""
name: myapp
fraises:
  api:
    type: api
    environments:
      dev:
        app_path: /var/www/dev
        systemd_service: bad/name
scaffold:
  deploy_user: myapp_deploy
  output_dir: {tmp_path / "output"}
""")
        with pytest.raises(ValidationError, match="systemd_service"):
            eager_load(p)

    def test_default_env_still_uses_generated_name(self, tmp_path):
        """Environments without systemd_service use the default pattern."""
        yaml = self._base_yaml(tmp_path, "api.dev.example.com.service")
        out = self._render(tmp_path, yaml)
        assert (out / "systemd" / "myapp_api_production.service").exists()


class TestRcdServiceTemplates:
    """Rc.d service templates for FreeBSD."""

    def _make_config(self, tmp_path, yaml_content):
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_rcd_template_renders_basic_service(self, tmp_path):
        """Rc.d template produces valid rc.d script."""
        config = self._make_config(
            tmp_path,
            """
name: tp
service_manager: rc
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/myapi
        port: 8000
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        rcd_path = tmp_path / "output" / "rc.d" / "tp_my_api_production"
        assert rcd_path.exists()

        content = rcd_path.read_text()
        assert "#!/bin/sh" in content
        assert "name=tp_my_api_production" in content
        assert "command=/var/www/myapi/manage.py" in content or "command=" in content
        assert "rcvar=tp_my_api_production_enable" in content

    def test_rcd_template_with_env_vars(self, tmp_path):
        """Rc.d template includes environment variables."""
        config = self._make_config(
            tmp_path,
            """
name: tp
service_manager: rc
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/myapi
        env:
          DJANGO_SETTINGS_MODULE: myapi.settings.production
          DATABASE_URL: postgresql://localhost/mydb
scaffold:
  output_dir: {output}
""".format(output=str(tmp_path / "output")),
        )
        from fraisier.scaffold.renderer import ScaffoldRenderer

        renderer = ScaffoldRenderer(config)
        renderer.render()

        rcd_path = tmp_path / "output" / "rc.d" / "tp_my_api_production"
        content = rcd_path.read_text()
        assert 'export DJANGO_SETTINGS_MODULE="myapi.settings.production"' in content
        assert 'export DATABASE_URL="postgresql://localhost/mydb"' in content


class TestServerFilteredBootstrapScaffold:
    """Bootstrap renders only nginx/systemd files for the target server (#111)."""

    _MULTI_SERVER_YAML = """\
name: myapp
servers:
  server-a:
    machine_hostnames: [server-a-backend-01]
  server-b:
    machine_hostnames: [server-b-backend-01]

scaffold:
  deploy_user: deployer
  output_dir: {output_dir}
  nginx:
    ssl_provider: letsencrypt
environments:
  development:
    server: server-a
  production:
    server: server-b
fraises:
  api:
    type: api
    environments:
      development:
        app_path: /var/www/dev
        nginx:
          server_name: api.dev.example.com
      production:
        app_path: /var/www/prod
        nginx:
          server_name: api.example.com
"""

    def _render(self, tmp_path, server):
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        out = tmp_path / "output"
        p = tmp_path / "fraises.yaml"
        p.write_text(self._MULTI_SERVER_YAML.format(output_dir=out))
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config, server=server)
        renderer.render()
        return out

    def test_server_a_generates_all_nginx_configs(self, tmp_path):
        """ScaffoldRenderer with server-a generates nginx configs for all servers (#148).

        Per-env nginx configs are always a complete artifact so that running
        scaffold locally never leaves remote-server configs stale.
        """
        out = self._render(tmp_path, "server-a")
        assert (out / "nginx" / "api.dev.example.com.conf").exists()
        assert (out / "nginx" / "api.example.com.conf").exists()

    def test_server_b_generates_all_nginx_configs(self, tmp_path):
        """ScaffoldRenderer with server-b generates nginx configs for all servers (#148)."""
        out = self._render(tmp_path, "server-b")
        assert (out / "nginx" / "api.example.com.conf").exists()
        assert (out / "nginx" / "api.dev.example.com.conf").exists()

    @staticmethod
    def _installed_vhosts(out, hostname: str) -> str:
        """The vhosts install.sh actually copies when run on *hostname*.

        Asserted by running the script rather than by grepping it. The vhost
        install is driven from the artifact manifest, which describes the whole
        render — every per-env vhost is rendered on purpose (#148) — and each
        host installs its own via ``_env_active``. So a remote vhost now
        *appears* in the generated script, inside a guard that is false on this
        machine. Grepping for its absence would fail while the behaviour is
        right; grepping for the guard would pin the mechanism instead of the
        outcome. Running it pins the outcome.
        """
        import os
        import subprocess

        bin_dir = out.parent / f"bin-{hostname}"
        bin_dir.mkdir(exist_ok=True)
        fake = bin_dir / "hostname"
        fake.write_text(f"#!/bin/bash\necho {hostname}\n")
        fake.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

        result = subprocess.run(
            ["bash", str(out / "install.sh"), "--dry-run", "--scaffold-dir", str(out)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        return "\n".join(
            line for line in result.stdout.splitlines() if "sites-available" in line
        )

    def test_install_sh_only_installs_local_nginx_configs(self, tmp_path):
        """Run on server-a's machine, install.sh installs only the dev vhost."""
        out = self._render(tmp_path, "server-a")

        installed = self._installed_vhosts(out, "server-a-backend-01")

        assert "api.dev.example.com" in installed
        assert "api.example.com.conf" not in installed

    def test_install_sh_server_b_does_not_reference_dev_nginx(self, tmp_path):
        """Run on server-b's machine, install.sh installs only the prod vhost."""
        out = self._render(tmp_path, "server-b")

        installed = self._installed_vhosts(out, "server-b-backend-01")

        assert "api.example.com" in installed
        assert "api.dev.example.com" not in installed

    def test_server_a_only_generates_dev_deploy_socket(self, tmp_path):
        """ScaffoldRenderer with server-a generates dev deploy socket, not prod."""
        out = self._render(tmp_path, "server-a")
        systemd_dir = out / "systemd"
        dev_sockets = [f for f in systemd_dir.iterdir() if "development" in f.name]
        prod_sockets = [f for f in systemd_dir.iterdir() if "production" in f.name]
        assert dev_sockets
        assert not prod_sockets

    def test_gateway_conf_has_no_ssl_when_per_env_nginx(self, tmp_path):
        """gateway.conf must not contain HTTPS blocks when per-env nginx configs exist.

        Fix for #197: per-env gateway_env.conf files are self-contained virtual
        hosts.  gateway.conf should only contain shared directives (limit_req_zone,
        HTTP catch-all) so it is safe to install on every machine.

        Supersedes the #143 regression (server_name selection) — the whole HTTPS
        block is removed rather than picking the right server_name for it.
        """
        for server in ("server-a", "server-b"):
            out = self._render(tmp_path, server)
            content = (out / "nginx" / "gateway.conf").read_text()
            assert "listen 443" not in content
            assert "ssl_certificate" not in content
            # Shared directives must still be present
            assert "limit_req_zone" in content
            assert "listen 80" in content

    def test_per_env_nginx_has_correct_ssl_cert(self, tmp_path):
        """Per-env nginx configs contain the correct SSL cert for their environment."""
        out = self._render(tmp_path, "server-b")
        prod_content = (out / "nginx" / "api.example.com.conf").read_text()
        assert "server_name api.example.com" in prod_content
        assert (
            "ssl_certificate /etc/letsencrypt/live/api.example.com/fullchain.pem"
            in prod_content
        )


class TestServerScopedCollectors:
    """Collectors (allowed_services) are filtered by server."""

    _YAML = """\
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {output_dir}
environments:
  staging:
    server: server-a
  production:
    server: server-b
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /var/www/staging
        database:
          name: myapp_staging
          strategy: restore_migrate
          admin_url: postgresql:///postgres?host=/var/run/postgresql
          restore:
            backup_dir: /backup/prod
            backup_pattern: "*.dump"
      production:
        app_path: /var/www/prod
        database:
          name: myapp_prod
          strategy: migrate
"""

    def _render(self, tmp_path, server):
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(self._YAML.format(output_dir=tmp_path / "output"))
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config, server=server)
        renderer.render()
        return tmp_path / "output"

    def test_systemctl_wrapper_scoped_to_server(self, tmp_path):
        """systemctl wrapper on server-a lists only staging service."""
        out = self._render(tmp_path, "server-a")
        content = (out / "systemctl-wrapper.sh").read_text()
        assert "myproj_my_api_staging.service" in content
        assert "myproj_my_api_production.service" not in content

    def test_systemctl_helper_scoped_to_server(self, tmp_path):
        """systemctl-helper on server-a lists only staging service."""
        out = self._render(tmp_path, "server-a")
        helper = (
            out / "systemd" / "fraisier-myproj-systemctl-helper.service"
        ).read_text()
        assert "myproj_my_api_staging.service" in helper
        assert "myproj_my_api_production.service" not in helper

    def test_restore_staging_readwrite_uses_configured_app_path(self, tmp_path):
        """restore-staging ReadWritePaths uses actual app_path, not hardcoded /opt."""
        out = self._render(tmp_path, "server-a")
        content = (out / "systemd" / "restore-staging.service").read_text()
        assert "ReadWritePaths=/var/www/staging" in content
        rw_lines = [
            line for line in content.splitlines() if line.startswith("ReadWritePaths=")
        ]
        assert not any("/opt/" in line for line in rw_lines)

    def test_restore_staging_uses_socket_not_wrapper(self, tmp_path):
        """restore-staging uses FRAISIER_SYSTEMCTL_SOCKET, not WRAPPER."""
        out = self._render(tmp_path, "server-a")
        content = (out / "systemd" / "restore-staging.service").read_text()
        assert "FRAISIER_SYSTEMCTL_SOCKET" in content
        assert "FRAISIER_SYSTEMCTL_WRAPPER" not in content

    def test_deploy_checker_readwrite_uses_configured_app_path(self, tmp_path):
        """deploy-checker ReadWritePaths uses actual app_path, not hardcoded /opt."""
        out = self._render(tmp_path, "server-a")
        content = (out / "systemd" / "deploy-checker.service").read_text()
        assert "ReadWritePaths=/var/www/staging" in content
        rw_lines = [
            line for line in content.splitlines() if line.startswith("ReadWritePaths=")
        ]
        assert not any("/opt/" in line for line in rw_lines)

    def test_deploy_checker_uses_socket_not_wrapper(self, tmp_path):
        """deploy-checker uses FRAISIER_SYSTEMCTL_SOCKET, not WRAPPER."""
        out = self._render(tmp_path, "server-a")
        content = (out / "systemd" / "deploy-checker.service").read_text()
        assert "FRAISIER_SYSTEMCTL_SOCKET" in content
        assert "FRAISIER_SYSTEMCTL_WRAPPER" not in content


class TestDeduplication:
    """ReadWritePaths and sudoers are deduplicated across fraises."""

    _YAML = """\
name: myproj
scaffold:
  deploy_user: deployer
  output_dir: {output_dir}
environments:
  staging:
    server: server-a
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /var/www/staging
        database:
          name: myapp_staging
          strategy: restore_migrate
          admin_url: postgresql:///postgres?host=/var/run/postgresql
          restore:
            backup_dir: /backup/prod
            backup_pattern: "*.dump"
  my_etl:
    type: etl
    environments:
      staging:
        app_path: /var/www/staging
        database:
          name: myapp_staging
          strategy: rebuild
          admin_url: postgresql:///postgres?host=/var/run/postgresql
"""

    def _render(self, tmp_path):
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(self._YAML.format(output_dir=tmp_path / "output"))
        config = FraisierConfig(p)
        renderer = ScaffoldRenderer(config, server="server-a")
        renderer.render()
        return tmp_path / "output"

    def test_deploy_checker_no_duplicate_readwrite_paths(self, tmp_path):
        """deploy-checker emits each app_path only once even with multiple fraises."""
        out = self._render(tmp_path)
        content = (out / "systemd" / "deploy-checker.service").read_text()
        rw_lines = [
            line for line in content.splitlines() if line.startswith("ReadWritePaths=")
        ]
        staging_lines = [line for line in rw_lines if "/var/www/staging" in line]
        assert len(staging_lines) == 1

    def test_restore_staging_no_duplicate_readwrite_paths(self, tmp_path):
        """restore-staging emits each app_path only once."""
        out = self._render(tmp_path)
        content = (out / "systemd" / "restore-staging.service").read_text()
        rw_lines = [
            line for line in content.splitlines() if line.startswith("ReadWritePaths=")
        ]
        staging_lines = [line for line in rw_lines if "/var/www/staging" in line]
        assert len(staging_lines) == 1

    def test_sudoers_has_no_pg_wrapper_rules(self, tmp_path):
        """Sudoers does not emit any pg-wrapper / pgadmin rules (admin_url-only)."""
        out = self._render(tmp_path)
        content = (out / "sudoers").read_text()
        assert "pgadmin-myproj" not in content
        assert "pg-wrapper" not in content
        assert "(postgres)" not in content


class TestStaleSocketCleanup:
    """scaffold removes stale legacy deploy socket/service files — issue #149."""

    def _make_config(self, tmp_path, extra_yaml=""):
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
        app_path: /var/www/prod
      development:
        app_path: /var/www/dev
{extra_yaml}
"""
        )
        return FraisierConfig(p)

    def test_stale_generic_socket_removed_after_render(self, tmp_path):
        """Legacy fraisier-{env}.socket files are removed when scaffold rerenders."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        systemd_dir = tmp_path / "output" / "systemd"
        systemd_dir.mkdir(parents=True)

        # Pre-seed legacy files (as if generated by pre-0.7.1)
        legacy_socket = systemd_dir / "fraisier-production.socket"
        legacy_service = systemd_dir / "fraisier-production@.service"
        legacy_socket.write_text("[Socket]\nListenStream=/run/fraisier/deploy.sock\n")
        legacy_service.write_text("[Service]\nExecStart=/usr/bin/fraisier\n")

        config = self._make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        # Legacy files must be gone
        assert not legacy_socket.exists(), (
            "stale fraisier-production.socket not removed"
        )
        assert not legacy_service.exists(), (
            "stale fraisier-production@.service not removed"
        )

        # New fraise-specific files must exist
        assert (systemd_dir / "fraisier-my_api-production.socket").exists()
        assert (systemd_dir / "fraisier-my_api-production@.service").exists()

    def test_stale_socket_from_other_fraise_removed(self, tmp_path):
        """Stale fraisier-{other_fraise}-{env}.socket from renamed fraise is removed."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        systemd_dir = tmp_path / "output" / "systemd"
        systemd_dir.mkdir(parents=True)

        # Pre-seed a stale socket for a fraise that no longer exists
        stale = systemd_dir / "fraisier-old_api-production.socket"
        stale_svc = systemd_dir / "fraisier-old_api-production@.service"
        stale.write_text("[Socket]\n")
        stale_svc.write_text("[Service]\n")

        config = self._make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        assert not stale.exists(), (
            "stale fraisier-old_api-production.socket not removed"
        )
        assert not stale_svc.exists(), (
            "stale fraisier-old_api-production@.service not removed"
        )

    def test_managed_socket_files_not_removed(self, tmp_path):
        """Currently-rendered socket files are NOT removed."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        systemd_dir = tmp_path / "output" / "systemd"
        assert (systemd_dir / "fraisier-my_api-production.socket").exists()
        assert (systemd_dir / "fraisier-my_api-development.socket").exists()

    def test_helper_sockets_not_removed(self, tmp_path):
        """systemctl-helper socket is NOT removed by stale cleanup."""
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config = self._make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        systemd_dir = tmp_path / "output" / "systemd"
        helper_socket = systemd_dir / "fraisier-tp-systemctl-helper.socket"
        assert helper_socket.exists(), "systemctl-helper socket must not be removed"


class TestInstallShLegacySocketMigration:
    """install.sh migrates stale pre-0.7.1 generic socket units — issue #150."""

    def _render(self, tmp_path, fraises_yaml):
        from fraisier.scaffold.renderer import ScaffoldRenderer

        p = tmp_path / "fraises.yaml"
        p.write_text(fraises_yaml.format(output_dir=tmp_path / "output"))
        config = FraisierConfig(p)
        ScaffoldRenderer(config).render()
        return (tmp_path / "output" / "install.sh").read_text()

    def test_install_sh_disables_legacy_generic_socket(self, tmp_path):
        """install.sh stops and removes fraisier-{env}.socket when new name differs."""
        content = self._render(
            tmp_path,
            """
name: tp
scaffold:
  deploy_user: deployer
  output_dir: {output_dir}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
""",
        )
        # Must contain migration block for the legacy generic socket name
        assert "fraisier-production.socket" in content
        assert "disable" in content or "systemctl disable" in content
        assert "rm -f" in content

    def test_install_sh_no_migration_when_name_field_set(self, tmp_path):
        """install.sh skips migration when env has a name field (name-based socket)."""
        content = self._render(
            tmp_path,
            """
name: tp
scaffold:
  deploy_user: deployer
  output_dir: {output_dir}
fraises:
  my_api:
    type: api
    environments:
      production:
        name: api.example.com
        app_path: /var/www/prod
""",
        )
        # socket is fraisier-api.example.com.socket, legacy would be fraisier-production.socket
        # Migration should reference the legacy name
        assert "fraisier-production.socket" in content

    def test_install_sh_migration_guarded_by_scope_active(self, tmp_path):
        """install.sh migration is guarded by _scope_active.

        The legacy unit it removes belongs to one fraise, so the gate is
        the fraise-keyed one: another fraise's host must not disable a
        socket named after an environment it happens to share (#336).
        """
        content = self._render(
            tmp_path,
            """
name: tp
scaffold:
  deploy_user: deployer
  output_dir: {output_dir}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/prod
""",
        )
        lines = content.splitlines()
        # Find the migration block
        migration_lines = [
            i for i, line in enumerate(lines) if "fraisier-production.socket" in line
        ]
        assert migration_lines, "no migration block found"
        # Check that _env_active appears nearby (within 5 lines)
        idx = migration_lines[0]
        context_block = "\n".join(lines[max(0, idx - 5) : idx + 5])
        assert "_scope_active" in context_block

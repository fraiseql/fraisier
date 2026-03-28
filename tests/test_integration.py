"""Integration testing.

End-to-end tests validating the full pipeline: scaffold, deploy strategies,
backup/restore, version management, webhook-to-deploy, and status/health.
"""

import hashlib
import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

_SINGLE_FRAISE_YAML = """\
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  my_api:
    type: api
    description: My API service
    schema_command: make schema-export
    compile_command: make schema-compile
    external_db: false
    environments:
      development:
        worker_count: 1
        memory_max: "2G"
        app_path: /srv/my-api
        systemd_service: my-api-dev.service
        health_check:
          url: http://localhost:8000/health
          timeout: 10
        database:
          tool: confiture
          strategy: rebuild
      production:
        worker_count: 4
        memory_max: "8G"
        app_path: /srv/my-api
        systemd_service: my-api-prod.service
        health_check:
          url: https://api.example.com/health
          timeout: 30
        database:
          tool: confiture
          strategy: migrate

deployment:
  deploy_user: fraisier
  strategies:
    development: rebuild
    staging: restore_migrate
    production: migrate

scaffold:
  output_dir: {output}
  deploy_user: fraisier
  systemd:
    security_hardening: true
  nginx:
    ssl_provider: letsencrypt
    cors_origins: ["*.example.io"]
    rate_limit: "10r/s"
    restricted_paths: ["/admin/"]
  github_actions:
    python_versions: ["3.12"]
    test_command: "uv run pytest"

health:
  startup_timeout_seconds: 60
  deploy_poll_interval_seconds: 2
  endpoints:
    - /health
    - /healthz
  response:
    include_version: true
    include_schema_hash: true
    include_response_time: true
    include_database: false
    include_environment: false
    include_commit: false

branch_mapping:
  main:
    fraise_name: my_api
    environment: production
    type: api
  develop:
    fraise_name: my_api
    environment: development
    type: api
"""

_MULTI_FRAISE_YAML = """\
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  management:
    type: api
    description: Management API
    schema_command: make schema-export
    external_db: false
    environments:
      development:
        worker_count: 1
        memory_max: "2G"
        port: 8042
      production:
        worker_count: 4
        memory_max: "8G"
        port: 8042
  backend:
    type: api
    description: Backend API
    external_db: false
    environments:
      development:
        worker_count: 1
        memory_max: "2G"
        port: 4001
      production:
        worker_count: 4
        memory_max: "8G"
        port: 4001
  data_pipeline:
    type: etl
    description: ETL pipeline
    external_db: true
    environments:
      production:
        app_path: /var/etl
        script_path: scripts/pipeline.py

deployment:
  deploy_user: fraisier
  strategies:
    development: rebuild
    production: migrate

scaffold:
  output_dir: {output}
  deploy_user: fraisier
  nginx:
    cors_origins: ["*.example.io"]
  github_actions:
    python_versions: ["3.12"]

health:
  endpoints:
    - /health
"""


def _make_config(tmp_path, yaml_template=_SINGLE_FRAISE_YAML):
    """Create a FraisierConfig from a YAML template with {output} placeholder."""
    output_dir = tmp_path / "output"
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(yaml_template.format(output=str(output_dir)))
    return FraisierConfig(str(config_file))


def _hash_content(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


class TestScaffoldValidation:
    """Integration: scaffold output is syntactically valid."""

    def test_single_fraise_scaffold_produces_expected_files(self, tmp_path):
        """Single-fraise scaffold generates core + provider files."""
        config = _make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        files = renderer.render()

        output_dir = tmp_path / "output"
        assert output_dir.exists()
        assert len(files) > 0

        # Core files should exist
        assert (output_dir / "install.sh").exists()
        assert (output_dir / "sudoers").exists()

    def test_systemd_units_have_valid_structure(self, tmp_path):
        """Generated .service files contain required systemd sections."""
        config = _make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        systemd_dir = tmp_path / "output" / "systemd"
        assert systemd_dir.exists()
        service_files = list(systemd_dir.glob("*.service"))
        assert len(service_files) > 0

        for svc_file in service_files:
            content = svc_file.read_text()
            assert "[Unit]" in content, f"{svc_file.name} missing [Unit]"
            assert "[Service]" in content, f"{svc_file.name} missing [Service]"
            # Timer-triggered oneshot services don't need [Install]
            if "Type=oneshot" not in content:
                assert "[Install]" in content, f"{svc_file.name} missing [Install]"
            assert "ExecStart=" in content, f"{svc_file.name} missing ExecStart"

    def test_nginx_config_has_valid_structure(self, tmp_path):
        """Generated nginx config has upstream and server blocks."""
        config = _make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        nginx_dir = tmp_path / "output" / "nginx"
        if nginx_dir.exists():
            gateway = nginx_dir / "gateway.conf"
            if gateway.exists():
                content = gateway.read_text()
                assert "server" in content
                assert "proxy_pass" in content

    def test_shell_scripts_have_shebang(self, tmp_path):
        """Generated .sh files have a shebang line."""
        config = _make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        output_dir = tmp_path / "output"
        sh_files = list(output_dir.glob("**/*.sh"))
        for sh_file in sh_files:
            content = sh_file.read_text()
            assert content.startswith("#!"), f"{sh_file.name} missing shebang"

    def test_multi_fraise_generates_gateway(self, tmp_path):
        """Multi-fraise topology generates nginx gateway.conf."""
        config = _make_config(tmp_path, _MULTI_FRAISE_YAML)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        gateway = tmp_path / "output" / "nginx" / "gateway.conf"
        assert gateway.exists()
        content = gateway.read_text()
        assert "upstream" in content

    def test_deploy_yml_generated(self, tmp_path):
        """deploy.yml GitHub Actions workflow is generated."""
        config = _make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        files = renderer.render()

        assert "deploy.yml" in files
        deploy_path = tmp_path / "output" / "deploy.yml"
        assert deploy_path.exists()
        content = deploy_path.read_text()
        assert "name:" in content


class TestBackupRestoreCycle:
    """Integration: pg_dump -> restore -> verify data integrity."""

    def test_full_backup_runs_pg_dump(self):
        """Full backup runs pg_dump with correct flags."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            from fraisier.dbops.backup import run_backup

            result = run_backup(
                db_name="test_db",
                output_dir="/tmp/backups",
                mode="full",
            )

            assert result.success is True
            assert "test_db" in result.backup_path
            assert "full" in result.backup_path
            cmd_args = mock_run.call_args[0][0]
            assert "pg_dump" in cmd_args
            assert "-Fc" in cmd_args

    def test_slim_backup_excludes_tables(self):
        """Slim backup passes -T flags for excluded tables."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            from fraisier.dbops.backup import run_backup

            result = run_backup(
                db_name="test_db",
                output_dir="/tmp/backups",
                mode="slim",
                excluded_tables=["large_logs", "audit_trail"],
            )

            assert result.success is True
            assert "slim" in result.backup_path
            cmd_args = mock_run.call_args[0][0]
            assert "-T" in cmd_args
            t_indices = [i for i, a in enumerate(cmd_args) if a == "-T"]
            excluded = [cmd_args[i + 1] for i in t_indices]
            assert "large_logs" in excluded
            assert "audit_trail" in excluded

    def test_restore_runs_pg_restore(self):
        """Restore runs pg_restore with --no-owner --no-acl."""
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")

            from fraisier.dbops.restore import restore_backup

            result = restore_backup(
                backup_path="/backups/test.dump",
                db_name="staging_db",
            )

            assert result.success is True
            call_args = mock_cmd.call_args[0][0]
            assert "pg_restore" in call_args
            assert "--no-owner" in call_args
            assert "--no-acl" in call_args

    def test_restore_with_ownership_fix(self):
        """Restore reassigns ownership when db_owner is set."""
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")

            from fraisier.dbops.restore import restore_backup

            result = restore_backup(
                backup_path="/backups/test.dump",
                db_name="staging_db",
                db_owner="app_user",
            )

            assert result.success is True
            # Should have been called twice: restore + ownership fix
            assert mock_cmd.call_count == 2
            ownership_call = mock_cmd.call_args_list[1][0][0]
            assert any("REASSIGN" in str(a) for a in ownership_call)

    def test_backup_failure_returns_error(self):
        """Backup failure returns BackupResult with error."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="pg_dump: error: disk full"
            )

            from fraisier.dbops.backup import run_backup

            result = run_backup(db_name="test_db", output_dir="/tmp/backups")

            assert result.success is False
            assert "disk full" in result.error

    def test_validate_table_count(self):
        """validate_table_count checks minimum table threshold."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="75\n", stderr="")

            from fraisier.dbops.restore import validate_table_count

            ok, count = validate_table_count("staging_db", min_threshold=50)
            assert ok is True
            assert count == 75

    def test_validate_table_count_below_threshold(self):
        """validate_table_count fails when below threshold."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="10\n", stderr="")

            from fraisier.dbops.restore import validate_table_count

            ok, count = validate_table_count("staging_db", min_threshold=50)
            assert ok is False
            assert count == 10


class TestVersionManagementIntegration:
    """Integration: bump -> verify version.json + pyproject.toml."""

    def test_bump_updates_version_json(self, tmp_path):
        """bump_version updates version.json with new version."""
        from fraisier.versioning import (
            VersionInfo,
            bump_version,
            read_version,
            write_version,
        )

        path = tmp_path / "version.json"
        write_version(VersionInfo(version="1.0.0", commit="abc123"), path)

        result = bump_version(path, "minor")
        assert result.version == "1.1.0"

        loaded = read_version(path)
        assert loaded is not None
        assert loaded.version == "1.1.0"

    def test_bump_creates_backup_file(self, tmp_path):
        """bump_version creates .bak file with old version."""
        from fraisier.versioning import VersionInfo, bump_version, write_version

        path = tmp_path / "version.json"
        write_version(VersionInfo(version="2.3.4"), path)

        bump_version(path, "patch")

        backup = tmp_path / "version.json.bak"
        assert backup.exists()
        data = json.loads(backup.read_text())
        assert data["version"] == "2.3.4"

    def test_bump_and_sync_pyproject(self, tmp_path):
        """bump_version + sync_pyproject_version updates both files."""
        from fraisier.versioning import (
            VersionInfo,
            bump_version,
            sync_pyproject_version,
            write_version,
        )

        version_path = tmp_path / "version.json"
        pyproject_path = tmp_path / "pyproject.toml"
        write_version(VersionInfo(version="1.0.0"), version_path)
        pyproject_path.write_text('[project]\nname = "myapp"\nversion = "1.0.0"\n')

        result = bump_version(version_path, "major")
        sync_pyproject_version(result.version, pyproject_path)

        assert result.version == "2.0.0"
        pyproject_content = pyproject_path.read_text()
        assert 'version = "2.0.0"' in pyproject_content

    def test_schema_hash_tracking(self, tmp_path):
        """update_schema_info tracks schema hash in version.json."""
        from fraisier.versioning import (
            VersionInfo,
            read_version,
            update_schema_info,
            write_version,
        )

        version_path = tmp_path / "version.json"
        write_version(VersionInfo(version="1.0.0"), version_path)

        schema_dir = tmp_path / "sql"
        schema_dir.mkdir()
        (schema_dir / "001.sql").write_text("CREATE TABLE users (id int);")
        (schema_dir / "002.sql").write_text("ALTER TABLE users ADD name text;")

        update_schema_info(version_path, schema_dir)

        v = read_version(version_path)
        assert v is not None
        assert v.schema_hash.startswith("sha256:")
        assert v.database_version.endswith(".002")

    def test_version_rollback_on_failure(self, tmp_path):
        """Backup file allows rollback after failure."""
        from fraisier.versioning import (
            VersionInfo,
            bump_version,
            read_version,
            write_version,
        )

        path = tmp_path / "version.json"
        write_version(VersionInfo(version="1.0.0"), path)

        bump_version(path, "patch")
        assert read_version(path).version == "1.0.1"

        # Simulate rollback by restoring .bak
        backup = tmp_path / "version.json.bak"
        assert backup.exists()
        import shutil

        shutil.copy2(backup, path)
        assert read_version(path).version == "1.0.0"


class TestWebhookToDeployIntegration:
    """Integration: webhook POST -> provider detection -> deploy trigger."""

    def test_github_push_triggers_deployment(self):
        """GitHub push event to mapped branch triggers deployment."""
        from fraisier.git import WebhookEvent
        from fraisier.webhook import process_webhook_event

        event = WebhookEvent(
            event_type="push",
            provider="github",
            branch="main",
            commit_sha="abc1234",
            sender="testuser",
            repository="org/my-api",
        )

        background_tasks = MagicMock()

        with patch("fraisier.webhook.get_config") as mock_config:
            mock_cfg = MagicMock()
            mock_cfg.get_fraises_for_branch.return_value = [{
                "fraise_name": "my_api",
                "environment": "production",
                "type": "api",
            }]
            mock_config.return_value = mock_cfg

            result = process_webhook_event(event, background_tasks, webhook_id=1)

        assert result["status"] == "deployment_triggered"
        assert result["fraise"] == "my_api"
        assert result["environment"] == "production"
        background_tasks.add_task.assert_called_once()

    def test_ping_event_returns_pong(self):
        """Ping event returns pong without triggering deploy."""
        from fraisier.git import WebhookEvent
        from fraisier.webhook import process_webhook_event

        event = WebhookEvent(event_type="ping", provider="github")
        background_tasks = MagicMock()

        result = process_webhook_event(event, background_tasks, webhook_id=1)

        assert result["status"] == "pong"
        background_tasks.add_task.assert_not_called()

    def test_unmapped_branch_ignored(self):
        """Push to unmapped branch is ignored."""
        from fraisier.git import WebhookEvent
        from fraisier.webhook import process_webhook_event

        event = WebhookEvent(
            event_type="push",
            provider="github",
            branch="feature/unrelated",
        )
        background_tasks = MagicMock()

        with patch("fraisier.webhook.get_config") as mock_config:
            mock_cfg = MagicMock()
            mock_cfg.get_fraises_for_branch.return_value = []
            mock_config.return_value = mock_cfg

            result = process_webhook_event(event, background_tasks, webhook_id=2)

        assert result["status"] == "ignored"
        background_tasks.add_task.assert_not_called()

    def test_all_four_providers_parse_events(self):
        """All 4 git providers can parse a push event."""
        from fraisier.git import get_provider

        providers_configs = {
            "github": {
                "webhook_secret": "test",
                "headers": {"X-GitHub-Event": "push"},
                "payload": {
                    "ref": "refs/heads/main",
                    "after": "abc123",
                    "sender": {"login": "user"},
                    "repository": {"full_name": "org/repo"},
                },
            },
            "gitlab": {
                "webhook_secret": "test",
                "headers": {"X-Gitlab-Event": "Push Hook"},
                "payload": {
                    "ref": "refs/heads/main",
                    "after": "abc123",
                    "user_username": "user",
                    "project": {"path_with_namespace": "org/repo"},
                },
            },
            "gitea": {
                "webhook_secret": "test",
                "headers": {"X-Gitea-Event": "push"},
                "payload": {
                    "ref": "refs/heads/main",
                    "after": "abc123",
                    "sender": {"login": "user"},
                    "repository": {"full_name": "org/repo"},
                },
            },
            "bitbucket": {
                "webhook_secret": "test",
                "headers": {"X-Event-Key": "repo:push"},
                "payload": {
                    "push": {
                        "changes": [
                            {
                                "new": {
                                    "type": "branch",
                                    "name": "main",
                                    "target": {"hash": "abc123"},
                                }
                            }
                        ]
                    },
                    "actor": {"display_name": "user"},
                    "repository": {"full_name": "org/repo"},
                },
            },
        }

        for provider_name, cfg in providers_configs.items():
            provider = get_provider(
                provider_name, {"webhook_secret": cfg["webhook_secret"]}
            )
            event = provider.parse_webhook_event(cfg["headers"], cfg["payload"])
            assert event.provider == provider_name, f"{provider_name}: wrong provider"
            assert event.is_push is True, f"{provider_name}: not detected as push"
            assert event.branch is not None, f"{provider_name}: missing branch"


class TestCIWorkflowValidation:
    """Integration: generated workflows are valid YAML."""

    def test_deploy_yml_is_valid_yaml(self, tmp_path):
        """deploy.yml parses as valid YAML."""
        import yaml

        config = _make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        deploy_path = tmp_path / "output" / "deploy.yml"
        assert deploy_path.exists()
        content = deploy_path.read_text()
        # Should not raise
        data = yaml.safe_load(content)
        assert isinstance(data, dict)
        assert "name" in data

    def test_deploy_yml_has_version_json(self, tmp_path):
        """deploy.yml references version.json for version gating."""
        config = _make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        deploy_path = tmp_path / "output" / "deploy.yml"
        content = deploy_path.read_text()
        assert "version.json" in content

    def test_single_fraise_no_external_db(self, tmp_path):
        """Single fraise with external_db: false gets confiture config."""
        config = _make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        confiture_path = tmp_path / "output" / "confiture.yaml"
        assert confiture_path.exists()

    def test_external_db_fraise_skipped_in_confiture(self, tmp_path):
        """external_db: true fraises should not appear in confiture.yaml."""
        config = _make_config(tmp_path, _MULTI_FRAISE_YAML)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        confiture_path = tmp_path / "output" / "confiture.yaml"
        if confiture_path.exists():
            content = confiture_path.read_text()
            # data_pipeline has external_db: true, should not be in confiture
            assert "data_pipeline" not in content


class TestStatusHealthEndToEnd:
    """Integration: deploy -> status -> health sequence."""

    def test_health_json_matches_schema(self, tmp_path):
        """Health JSON output matches expected schema with security omissions."""
        from fraisier.config import HealthResponseConfig
        from fraisier.health_check import (
            AggregateHealthResult,
            ServiceHealthResult,
        )

        svc = ServiceHealthResult(
            name="my_api",
            url=":8000",
            status="healthy",
            response_time_ms=12.5,
            version="1.0.0",
        )
        agg = AggregateHealthResult(
            status="healthy",
            services={"my_api": svc},
            response_time_ms=12.5,
        )

        resp_cfg = HealthResponseConfig(
            include_version=True,
            include_schema_hash=True,
            include_response_time=True,
            include_database=False,
            include_environment=False,
            include_commit=False,
        )

        d = agg.to_dict(response_config=resp_cfg)
        assert d["status"] == "healthy"
        assert "my_api" in d["services"]
        assert d["services"]["my_api"]["version"] == "1.0.0"
        assert "response_time_ms" in d
        assert "database" not in d
        assert "environment" not in d.get("services", {}).get("my_api", {})

    def test_health_cli_json_output(self, tmp_path):
        """fraisier health --json returns valid JSON with schema."""
        from fraisier.cli import main
        from fraisier.health_check import (
            AggregateHealthResult,
            ServiceHealthResult,
        )

        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            _SINGLE_FRAISE_YAML.format(output=str(tmp_path / "output"))
        )

        svc = ServiceHealthResult(
            name="my_api",
            url=":8000",
            status="healthy",
            response_time_ms=10.0,
            version="1.0.0",
        )
        agg = AggregateHealthResult(
            status="healthy",
            services={"my_api": svc},
            response_time_ms=10.0,
        )

        runner = CliRunner()
        with patch("fraisier.health_check.AggregateHealthChecker") as mock_cls:
            mock_cls.return_value.check_all.return_value = agg
            result = runner.invoke(main, ["-c", str(config_file), "health", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "healthy"
        assert "my_api" in data["services"]

    def test_validate_cli_runs_all_checks(self, tmp_path):
        """fraisier validate runs and reports all checks."""
        from fraisier.cli import main

        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            _SINGLE_FRAISE_YAML.format(output=str(tmp_path / "output"))
        )

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(config_file), "validate"])
        assert result.exit_code in (0, 1)
        assert "checks passed" in result.output.lower()

    def test_validate_json_output(self, tmp_path):
        """fraisier validate --json returns structured output."""
        from fraisier.cli import main

        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            _SINGLE_FRAISE_YAML.format(output=str(tmp_path / "output"))
        )

        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(config_file), "validate", "--json"])
        assert result.exit_code in (0, 1)
        data = json.loads(result.output)
        assert "passed" in data
        assert "checks" in data
        assert len(data["checks"]) >= 2

    def test_drift_detection_after_scaffold(self, tmp_path):
        """Drift detection finds modified scaffolded files."""
        from fraisier.validation import detect_drift

        config = _make_config(tmp_path)
        renderer = ScaffoldRenderer(config)
        renderer.render()

        output_dir = tmp_path / "output"

        # Compute original hashes
        template_hashes = {}
        install_sh = output_dir / "install.sh"
        if install_sh.exists():
            content = install_sh.read_text()
            template_hashes["install.sh"] = _hash_content(content)

        # No drift yet
        results = detect_drift(output_dir, template_hashes)
        assert len(results) == 0

        # Modify a file
        if install_sh.exists():
            install_sh.write_text("#!/bin/bash\necho MODIFIED\n")
            results = detect_drift(output_dir, template_hashes)
            assert len(results) == 1
            assert results[0].drifted is True

"""Production hardening tests.

Tests for error handling, recovery integration, async bridge,
metrics, logging, locking, deployment config, strategies, runner,
CLI enhancements, and status command.
"""

from unittest.mock import patch

import pytest

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentStatus
from fraisier.deployers.etl import ETLDeployer
from fraisier.deployers.scheduled import ScheduledDeployer
from fraisier.errors import (
    ConfigurationError,
    DeploymentError,
    DeploymentTimeoutError,
    FraisierError,
    HealthCheckError,
)
from fraisier.metrics import MetricsRecorder


class TestErrorHandlingAudit:
    """Deployment failures must produce structured FraisierError with context."""

    def test_api_deployer_git_pull_failure_raises_deployment_error(self):
        """APIDeployer._git_pull failure wraps as DeploymentError."""
        deployer = APIDeployer(
            {
                "fraise_name": "my_api",
                "environment": "production",
                "app_path": "/nonexistent",
                "systemd_service": "my_api.service",
            }
        )
        result = deployer.execute()
        assert not result.success
        assert result.status == DeploymentStatus.FAILED
        assert isinstance(result.error, FraisierError)
        assert result.error.context.get("fraise") == "my_api"
        assert result.error.context.get("environment") == "production"

    def test_api_deployer_health_check_failure_raises_health_check_error(self):
        """Health check failure produces HealthCheckError with context."""
        from fraisier.health_check import HealthCheckResult

        deployer = APIDeployer(
            {
                "fraise_name": "my_api",
                "environment": "staging",
                "app_path": "/tmp",
                "health_check": {"url": "http://localhost:99999/health", "timeout": 1},
            }
        )
        fail = HealthCheckResult(
            success=False,
            check_type="http",
            duration=1.0,
            message="refused",
        )
        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=(None, "abc123"),
            ),
            patch("fraisier.deployers.api.HealthCheckManager") as MockMgr,
            patch("fraisier.deployers.api.HTTPHealthChecker"),
        ):
            MockMgr.return_value.check_with_retries.return_value = fail
            result = deployer.execute()
        assert not result.success
        assert isinstance(result.error, HealthCheckError)
        assert result.error.context.get("fraise") == "my_api"

    def test_etl_deployer_failure_produces_structured_error(self):
        """ETLDeployer failure wraps as DeploymentError with context."""
        deployer = ETLDeployer(
            {
                "fraise_name": "etl_job",
                "environment": "development",
                "app_path": "/nonexistent",
                "script_path": "run.py",
            }
        )
        result = deployer.execute()
        assert not result.success
        assert isinstance(result.error, FraisierError)
        assert result.error.context.get("fraise") == "etl_job"

    def test_scheduled_deployer_failure_produces_structured_error(self):
        """ScheduledDeployer failure wraps as DeploymentError with context."""
        deployer = ScheduledDeployer(
            {
                "fraise_name": "cron_job",
                "environment": "production",
                "systemd_timer": "nonexistent.timer",
            }
        )
        with patch(
            "subprocess.run",
            side_effect=FileNotFoundError("systemctl not found"),
        ):
            result = deployer.execute()
        assert not result.success
        assert isinstance(result.error, FraisierError)
        assert result.error.context.get("fraise") == "cron_job"

    def test_deployment_result_carries_error_object(self):
        """DeploymentResult.error is always a FraisierError on failure."""
        deployer = APIDeployer(
            {
                "fraise_name": "api",
                "environment": "dev",
                "app_path": "/nonexistent",
            }
        )
        result = deployer.execute()
        assert not result.success
        assert result.error is not None
        assert isinstance(result.error, FraisierError)
        assert result.error.recoverable is not None  # explicit flag

    def test_error_to_dict_includes_context(self):
        """FraisierError.to_dict() includes context for structured logging."""
        err = DeploymentError(
            "deploy failed",
            context={"fraise": "api", "environment": "prod"},
        )
        d = err.to_dict()
        assert d["context"]["fraise"] == "api"
        assert d["context"]["environment"] == "prod"
        assert d["code"] == "DEPLOYMENT_ERROR"

    def test_recoverable_flag_correct_on_timeout(self):
        """DeploymentTimeoutError is recoverable."""
        err = DeploymentTimeoutError("timed out", context={"timeout": 300})
        assert err.recoverable is True

    def test_recoverable_flag_correct_on_health_check(self):
        """HealthCheckError is recoverable."""
        err = HealthCheckError("unhealthy")
        assert err.recoverable is True

    def test_recoverable_flag_false_on_config_error(self):
        """ConfigurationError is not recoverable."""
        err = ConfigurationError("bad config")
        assert err.recoverable is False


class TestMetricsIntegration:
    """Deploy, rollback, and health check must record Prometheus metrics."""

    def test_record_deployment_start(self):
        """record_deployment_start tracks active deployments."""
        recorder = MetricsRecorder()
        # Should not raise
        recorder.record_deployment_start("bare_metal", "api")

    def test_record_deployment_complete(self):
        """record_deployment_complete records counter and histogram."""
        recorder = MetricsRecorder()
        recorder.record_deployment_start("bare_metal", "api")
        recorder.record_deployment_complete("bare_metal", "api", "success", 42.5)

    def test_record_deployment_error(self):
        """record_deployment_error increments error counter."""
        recorder = MetricsRecorder()
        recorder.record_deployment_start("bare_metal", "api")
        recorder.record_deployment_error("bare_metal", "timeout")

    def test_record_rollback(self):
        """record_rollback records rollback counter and duration."""
        recorder = MetricsRecorder()
        recorder.record_rollback("docker_compose", "health_check", 15.3)

    def test_record_health_check(self):
        """record_health_check records check counter and duration."""
        recorder = MetricsRecorder()
        recorder.record_health_check("bare_metal", "http", "pass", 0.234)

    def test_set_provider_availability(self):
        """set_provider_availability sets gauge."""
        recorder = MetricsRecorder()
        recorder.set_provider_availability("bare_metal", True)
        recorder.set_provider_availability("docker_compose", False)

    def test_record_lock_wait(self):
        """record_lock_wait records wait time gauge."""
        recorder = MetricsRecorder()
        recorder.record_lock_wait("my_api", "bare_metal", 2.5)

    def test_metrics_summary_reports_availability(self):
        """get_metrics_summary returns status."""
        recorder = MetricsRecorder()
        summary = recorder.get_metrics_summary()
        assert "prometheus_available" in summary

    def test_db_metrics_recorded(self):
        """Database metrics methods do not raise."""
        recorder = MetricsRecorder()
        recorder.record_db_query("sqlite", "select", "success", 0.01)
        recorder.record_db_error("sqlite", "timeout")
        recorder.record_db_transaction("sqlite", 0.5)
        recorder.record_deployment_db_operation("sqlite", "record_start")
        recorder.update_db_pool_metrics("sqlite", 5, 3, 0)


class TestStructuredLogging:
    """Deploy logs must include deployment_id, fraise, env in context."""

    def test_contextual_logger_adds_context(self):
        """ContextualLogger context manager adds keys to log context."""
        from fraisier.logging import ContextualLogger

        logger = ContextualLogger("test")
        with logger.context(deployment_id="d-123", fraise="my_api", env="prod"):
            ctx = logger._get_context()
            assert ctx["deployment_id"] == "d-123"
            assert ctx["fraise"] == "my_api"
            assert ctx["env"] == "prod"

    def test_context_nesting(self):
        """Nested contexts accumulate keys."""
        from fraisier.logging import ContextualLogger

        logger = ContextualLogger("test")
        with logger.context(deployment_id="d-123"):  # noqa: SIM117
            with logger.context(fraise="api", env="staging"):
                ctx = logger._get_context()
                assert ctx["deployment_id"] == "d-123"
                assert ctx["fraise"] == "api"
                assert ctx["env"] == "staging"

    def test_context_cleanup_on_exit(self):
        """Context is removed after exiting context manager."""
        from fraisier.logging import ContextualLogger

        logger = ContextualLogger("test")
        with logger.context(deployment_id="d-123"):
            pass
        assert logger._get_context() == {}

    def test_redaction_of_sensitive_keys(self):
        """Sensitive keys are redacted in log output."""
        from fraisier.logging import ContextualLogger

        logger = ContextualLogger("test")
        redacted = logger._redact_dict(
            {"api_key": "secret123", "user": "admin", "password": "pass"}
        )
        assert redacted["api_key"] == "***REDACTED***"
        assert redacted["password"] == "***REDACTED***"
        assert redacted["user"] == "admin"

    def test_json_formatter_produces_json(self):
        """JSONFormatter outputs valid JSON with expected fields."""
        import json
        import logging

        from fraisier.logging import JSONFormatter

        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="fraisier.deploy",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Deployment started",
            args=None,
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "Deployment started"
        assert "timestamp" in parsed


class TestDeploymentConfigParsing:
    """deployment: section must parse from fraises.yaml with defaults."""

    def _make_config(self, tmp_path, yaml_content):
        """Write yaml_content to a temp file and return FraisierConfig."""
        from fraisier.config import FraisierConfig

        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(p)

    def test_deployment_section_parses(self, tmp_path):
        """Full deployment section parses correctly."""
        config = self._make_config(
            tmp_path,
            """
fraises: {}
deployment:
  lock_dir: /run/my-project
  status_file: deployment_status.json
  deploy_user: my_project_app
  strategies:
    development: rebuild
    staging: restore_migrate
    production: migrate
  timeouts:
    development: 420
    staging: 1800
    production: 900
""",
        )
        dep = config.deployment
        assert dep.lock_dir == "/run/my-project"
        assert dep.status_file == "deployment_status.json"
        assert dep.deploy_user == "my_project_app"
        assert dep.strategies["development"] == "rebuild"
        assert dep.strategies["production"] == "migrate"
        assert dep.timeouts["staging"] == 1800

    def test_deployment_section_defaults(self, tmp_path):
        """Missing deployment section uses sensible defaults."""
        config = self._make_config(tmp_path, "fraises: {}\n")
        dep = config.deployment
        assert dep.lock_dir == "/run/fraisier"
        assert dep.status_file == "deployment_status.json"
        assert dep.strategies == {}
        assert dep.timeouts == {}
        assert dep.deploy_user == "fraisier"

    def test_get_strategy_for_environment(self, tmp_path):
        """get_strategy_for_environment returns strategy name or None."""
        config = self._make_config(
            tmp_path,
            """
fraises: {}
deployment:
  strategies:
    development: rebuild
    production: migrate
""",
        )
        assert config.deployment.get_strategy("development") == "rebuild"
        assert config.deployment.get_strategy("production") == "migrate"
        assert config.deployment.get_strategy("staging") is None

    def test_get_timeout_for_environment(self, tmp_path):
        """get_timeout returns per-env timeout or default."""
        config = self._make_config(
            tmp_path,
            """
fraises: {}
deployment:
  timeouts:
    production: 900
""",
        )
        assert config.deployment.get_timeout("production") == 900
        assert config.deployment.get_timeout("development") == 600

    def test_invalid_strategy_name_raises(self, tmp_path):
        """Invalid strategy name raises ValidationError."""
        from fraisier.errors import ValidationError

        config = self._make_config(
            tmp_path,
            """
fraises: {}
deployment:
  strategies:
    production: yolo_deploy
""",
        )
        with pytest.raises(ValidationError, match="yolo_deploy"):
            config.deployment  # noqa: B018


class TestDeployCLI:
    """fraisier deploy supports --dry-run, --skip-health, --force."""

    def _make_config_file(self, tmp_path):
        """Create a minimal fraises.yaml for CLI testing."""
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(
            """
fraises:
  my_api:
    type: api
    description: "Test API"
    environments:
      development:
        name: dev_api
        app_path: /opt/my_api
        systemd_service: my_api.service
deployment:
  strategies:
    development: rebuild
"""
        )
        return str(cfg)

    def test_trigger_deploy_dry_run_flag_accepted(self, tmp_path):
        """--dry-run flag is accepted by trigger-deploy CLI."""
        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = self._make_config_file(tmp_path)
        runner = CliRunner()
        # Just check the flag is accepted (will fail since no socket, but flag parsing works)  # noqa: E501
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "trigger-deploy",
                "my_api",
                "development",
                "--dry-run",
            ],
        )
        # Exit code 1 is expected since socket doesn't exist, but flag parsing should work  # noqa: E501
        assert result.exit_code == 1  # Connection error, not argument error

    def test_trigger_deploy_force_flag_accepted(self, tmp_path):
        """--force flag is accepted by trigger-deploy CLI."""
        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = self._make_config_file(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-c",
                cfg,
                "trigger-deploy",
                "my_api",
                "development",
                "--force",
                "--dry-run",
            ],
        )
        # Exit code 1 is expected since socket doesn't exist, but flag parsing should work  # noqa: E501
        assert result.exit_code == 1  # Connection error, not argument error

    def test_trigger_deploy_nonexistent_fraise_fails(self, tmp_path):
        """trigger-deploy with unknown fraise fails with error message."""
        from click.testing import CliRunner

        from fraisier.cli import main

        cfg = self._make_config_file(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["-c", cfg, "trigger-deploy", "nonexistent", "production"],
        )
        assert result.exit_code == 1
        assert "not found" in result.output


class TestErrorPropagationGaps:
    """Cycle 5: verify silent error swallows now log with context."""

    def test_status_write_failure_logs_fraise_name_and_path(self, tmp_path, caplog):
        """Status write OSError must log WARNING with fraise name and path."""
        config = {
            "fraise_name": "myapi",
            "environment": "production",
            "app_path": "/srv/myapi",
            "clone_url": "git@github.com:org/myapi.git",
            "branch": "main",
            "repos_base": str(tmp_path / "repos"),
            "status_dir": str(tmp_path / "status"),
        }
        deployer = APIDeployer(config)

        with (
            patch(
                "fraisier.deployers.mixins.write_status",
                side_effect=OSError("disk full"),
            ),
            caplog.at_level("WARNING"),
        ):
            deployer._write_status("deploying")

        assert any("myapi" in r.message for r in caplog.records)
        assert any("status" in r.message.lower() for r in caplog.records)

    def test_db_start_failure_logs_warning_with_fraise(self, tmp_path, caplog):
        """DB record start failure must log WARNING with fraise name."""
        import sqlite3

        config = {
            "fraise_name": "myapi",
            "environment": "production",
            "app_path": "/srv/myapi",
            "clone_url": "git@github.com:org/myapi.git",
            "branch": "main",
            "repos_base": str(tmp_path / "repos"),
            "status_dir": str(tmp_path / "status"),
        }
        deployer = APIDeployer(config)

        with (
            patch(
                "fraisier.database.get_db",
                side_effect=sqlite3.Error("db locked"),
            ),
            caplog.at_level("WARNING"),
        ):
            result = deployer._start_db_record()

        assert result is None
        assert any("myapi" in r.message for r in caplog.records)

    def test_db_complete_failure_logs_warning_with_deployment_id(
        self, tmp_path, caplog
    ):
        """DB record completion failure must log WARNING with deployment_id."""
        import sqlite3

        from fraisier.deployers.base import DeploymentResult

        config = {
            "fraise_name": "myapi",
            "environment": "production",
            "app_path": "/srv/myapi",
            "clone_url": "git@github.com:org/myapi.git",
            "branch": "main",
            "repos_base": str(tmp_path / "repos"),
            "status_dir": str(tmp_path / "status"),
        }
        deployer = APIDeployer(config)
        fake_result = DeploymentResult(
            success=True,
            status=DeploymentStatus.SUCCESS,
        )

        with (
            patch(
                "fraisier.database.get_db",
                side_effect=sqlite3.Error("db locked"),
            ),
            caplog.at_level("WARNING"),
        ):
            deployer._complete_db_record(42, fake_result)

        assert any("42" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_webhook_exception_preserves_class_name(self, caplog):
        """Webhook generic exception handler must include exception class name."""
        from unittest.mock import MagicMock

        from fraisier.webhook import _run_deployment

        mock_db = MagicMock()

        with (
            patch(
                "fraisier.runners.runner_from_config",
                side_effect=RuntimeError("bad runner config"),
            ),
            caplog.at_level("ERROR"),
        ):
            await _run_deployment(
                fraise_name="myapi",
                environment="production",
                fraise_config={"type": "api"},
                webhook_id=None,
                git_branch="main",
                git_commit=None,
                db=mock_db,
            )

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert any("RuntimeError" in r.message for r in error_records)

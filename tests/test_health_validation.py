"""Tests for composite health checks and validation."""

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner


class TestHealthResponseConfig:
    """Test HealthResponseConfig dataclass defaults and fields."""

    def test_default_security_first(self):
        from fraisier.config import HealthResponseConfig

        cfg = HealthResponseConfig()
        assert cfg.include_version is True
        assert cfg.include_schema_hash is True
        assert cfg.include_response_time is True
        assert cfg.include_database is False
        assert cfg.include_environment is False
        assert cfg.include_commit is False

    def test_custom_values(self):
        from fraisier.config import HealthResponseConfig

        cfg = HealthResponseConfig(
            include_version=False,
            include_database=True,
        )
        assert cfg.include_version is False
        assert cfg.include_database is True


class TestHealthConfig:
    """Test HealthConfig dataclass defaults and fields."""

    def test_defaults(self):
        from fraisier.config import HealthConfig

        cfg = HealthConfig()
        assert cfg.startup_timeout_seconds == 120
        assert cfg.deploy_poll_interval_seconds == 5
        assert cfg.endpoints == ["/health"]
        assert isinstance(cfg.response, object)

    def test_endpoints_list(self):
        from fraisier.config import HealthConfig

        cfg = HealthConfig(endpoints=["/health", "/healthz", "/readyz"])
        assert len(cfg.endpoints) == 3
        assert cfg.endpoints[0] == "/health"

    def test_response_nested(self):
        from fraisier.config import HealthConfig, HealthResponseConfig

        resp = HealthResponseConfig(include_version=False)
        cfg = HealthConfig(response=resp)
        assert cfg.response.include_version is False


class TestHealthConfigParsing:
    """Test that FraisierConfig parses the health section from YAML."""

    def test_parse_full_health_section(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
fraises:
  my_api:
    type: api
    description: Test
    environments:
      development:
        app_path: /tmp/test

health:
  startup_timeout_seconds: 60
  deploy_poll_interval_seconds: 10
  endpoints:
    - /health
    - /healthz
    - /readyz
  response:
    include_version: false
    include_schema_hash: false
    include_response_time: true
    include_database: false
    include_environment: false
    include_commit: false
"""
        )
        from fraisier.config import FraisierConfig

        cfg = FraisierConfig(str(config_file))
        health = cfg.health
        assert health.startup_timeout_seconds == 60
        assert health.deploy_poll_interval_seconds == 10
        assert health.endpoints == ["/health", "/healthz", "/readyz"]
        assert health.response.include_version is False
        assert health.response.include_schema_hash is False

    def test_parse_missing_health_returns_defaults(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
fraises:
  my_api:
    type: api
    description: Test
    environments:
      development:
        app_path: /tmp/test
"""
        )
        from fraisier.config import FraisierConfig

        cfg = FraisierConfig(str(config_file))
        health = cfg.health
        assert health.startup_timeout_seconds == 120
        assert health.deploy_poll_interval_seconds == 5
        assert health.endpoints == ["/health"]
        assert health.response.include_version is True
        assert health.response.include_database is False

    def test_parse_partial_health_response(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
fraises:
  my_api:
    type: api
    description: Test
    environments:
      development:
        app_path: /tmp/test

health:
  startup_timeout_seconds: 90
  response:
    include_version: false
"""
        )
        from fraisier.config import FraisierConfig

        cfg = FraisierConfig(str(config_file))
        health = cfg.health
        assert health.startup_timeout_seconds == 90
        assert health.endpoints == ["/health"]
        assert health.response.include_version is False
        assert health.response.include_database is False


class TestServiceHealthResult:
    """Test ServiceHealthResult dataclass."""

    def test_create_healthy(self):
        from fraisier.health_check import ServiceHealthResult

        r = ServiceHealthResult(
            name="backend",
            url=":4001",
            status="healthy",
            response_time_ms=12.5,
        )
        assert r.name == "backend"
        assert r.status == "healthy"
        assert r.version is None

    def test_create_with_version(self):
        from fraisier.health_check import ServiceHealthResult

        r = ServiceHealthResult(
            name="management",
            url=":8042",
            status="healthy",
            response_time_ms=5.0,
            version="1.2.3",
        )
        assert r.version == "1.2.3"


class TestAggregateHealthResult:
    """Test AggregateHealthResult dataclass."""

    def test_create_aggregate(self):
        from fraisier.health_check import (
            AggregateHealthResult,
            ServiceHealthResult,
        )

        svc = ServiceHealthResult(
            name="backend", url=":4001", status="healthy", response_time_ms=10.0
        )
        agg = AggregateHealthResult(
            status="healthy",
            services={"backend": svc},
            response_time_ms=10.0,
        )
        assert agg.status == "healthy"
        assert "backend" in agg.services

    def test_to_dict_matches_schema(self):
        from fraisier.health_check import (
            AggregateHealthResult,
            ServiceHealthResult,
        )

        svc = ServiceHealthResult(
            name="management",
            url=":8042",
            status="healthy",
            response_time_ms=5.0,
            version="1.2.3",
        )
        agg = AggregateHealthResult(
            status="healthy",
            services={"management": svc},
            response_time_ms=12.0,
        )
        d = agg.to_dict()
        assert d["status"] == "healthy"
        assert "management" in d["services"]
        assert d["services"]["management"]["url"] == ":8042"
        assert d["services"]["management"]["status"] == "healthy"
        assert d["services"]["management"]["version"] == "1.2.3"
        assert d["response_time_ms"] == 12.0

    def test_to_dict_applies_security_omissions(self):
        from fraisier.config import HealthResponseConfig
        from fraisier.health_check import (
            AggregateHealthResult,
            ServiceHealthResult,
        )

        svc = ServiceHealthResult(
            name="api",
            url=":4001",
            status="healthy",
            response_time_ms=10.0,
            version="1.0.0",
        )
        resp_cfg = HealthResponseConfig(
            include_version=False,
            include_response_time=False,
        )
        agg = AggregateHealthResult(
            status="healthy",
            services={"api": svc},
            response_time_ms=10.0,
        )
        d = agg.to_dict(response_config=resp_cfg)
        assert "version" not in d["services"]["api"]
        assert "response_time_ms" not in d

    def test_to_dict_never_includes_database(self):
        from fraisier.config import HealthResponseConfig
        from fraisier.health_check import (
            AggregateHealthResult,
            ServiceHealthResult,
        )

        svc = ServiceHealthResult(
            name="api", url=":4001", status="healthy", response_time_ms=5.0
        )
        resp_cfg = HealthResponseConfig(include_database=True)
        agg = AggregateHealthResult(
            status="healthy",
            services={"api": svc},
            response_time_ms=5.0,
        )
        d = agg.to_dict(response_config=resp_cfg)
        assert "database" not in d


class TestAggregateHealthChecker:
    """Test AggregateHealthChecker multi-service aggregation."""

    def test_all_healthy(self):
        from unittest.mock import patch as mock_patch

        from fraisier.config import HealthConfig
        from fraisier.health_check import AggregateHealthChecker

        checker = AggregateHealthChecker(
            services={
                "management": "http://localhost:8042",
                "backend": "http://localhost:4001",
            },
            health_config=HealthConfig(),
        )

        def mock_urlopen(url, *, timeout=5.0):
            m = MagicMock()
            m.status = 200
            m.read.return_value = b"{}"
            m.__enter__ = lambda s: s
            m.__exit__ = MagicMock(return_value=False)
            return m

        with mock_patch(
            "fraisier.health_check.urllib.request.urlopen", side_effect=mock_urlopen
        ):
            result = checker.check_all()

        assert result.status == "healthy"
        assert len(result.services) == 2
        assert result.services["management"].status == "healthy"
        assert result.services["backend"].status == "healthy"

    def test_partial_failure(self):
        import urllib.error
        from unittest.mock import patch as mock_patch

        from fraisier.config import HealthConfig
        from fraisier.health_check import AggregateHealthChecker

        checker = AggregateHealthChecker(
            services={
                "management": "http://localhost:8042",
                "backend": "http://localhost:4001",
            },
            health_config=HealthConfig(),
        )

        def mock_urlopen(url, *, timeout=5.0):
            if "4001" in url:
                raise urllib.error.URLError("Connection refused")
            m = MagicMock()
            m.status = 200
            m.read.return_value = b"{}"
            m.__enter__ = lambda s: s
            m.__exit__ = MagicMock(return_value=False)
            return m

        with mock_patch(
            "fraisier.health_check.urllib.request.urlopen", side_effect=mock_urlopen
        ):
            result = checker.check_all()

        assert result.status == "degraded"
        assert result.services["management"].status == "healthy"
        assert result.services["backend"].status == "unhealthy"

    def test_all_unhealthy(self):
        import urllib.error
        from unittest.mock import patch as mock_patch

        from fraisier.config import HealthConfig
        from fraisier.health_check import AggregateHealthChecker

        checker = AggregateHealthChecker(
            services={
                "management": "http://localhost:8042",
            },
            health_config=HealthConfig(),
        )

        def mock_urlopen(url, *, timeout=5.0):
            raise urllib.error.URLError("Connection refused")

        with mock_patch(
            "fraisier.health_check.urllib.request.urlopen", side_effect=mock_urlopen
        ):
            result = checker.check_all()

        assert result.status == "unhealthy"

    def test_tries_endpoints_in_order(self):
        import urllib.error
        from unittest.mock import patch as mock_patch

        from fraisier.config import HealthConfig
        from fraisier.health_check import AggregateHealthChecker

        cfg = HealthConfig(endpoints=["/health", "/healthz", "/readyz"])
        checker = AggregateHealthChecker(
            services={"api": "http://localhost:4001"},
            health_config=cfg,
        )

        attempted_urls = []

        def mock_urlopen(url, *, timeout=5.0):
            attempted_urls.append(url)
            if url.endswith("/readyz"):
                m = MagicMock()
                m.status = 200
                m.read.return_value = b"{}"
                m.__enter__ = lambda s: s
                m.__exit__ = MagicMock(return_value=False)
                return m
            raise urllib.error.URLError("Not found")

        with mock_patch(
            "fraisier.health_check.urllib.request.urlopen", side_effect=mock_urlopen
        ):
            result = checker.check_all()

        assert result.status == "healthy"
        assert "http://localhost:4001/health" in attempted_urls
        assert "http://localhost:4001/healthz" in attempted_urls
        assert "http://localhost:4001/readyz" in attempted_urls


class TestHealthCLI:
    """Test fraisier health CLI command."""

    def _make_config(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
fraises:
  management:
    type: api
    description: Management API
    environments:
      development:
        app_path: /tmp/mgmt
        port: 8042
  backend:
    type: api
    description: Backend API
    environments:
      development:
        app_path: /tmp/backend
        port: 4001

health:
  endpoints:
    - /health
"""
        )
        return str(config_file)

    def test_health_command_exists(self, tmp_path):
        from fraisier.cli import main

        runner = CliRunner()
        config_path = self._make_config(tmp_path)
        result = runner.invoke(main, ["-c", config_path, "health", "--help"])
        assert result.exit_code == 0
        assert "health" in result.output.lower()

    def test_health_table_output(self, tmp_path):
        from fraisier.cli import main

        runner = CliRunner()
        config_path = self._make_config(tmp_path)

        mock_result = MagicMock()
        mock_result.status = "healthy"
        mock_result.response_time_ms = 12.0
        mock_svc = MagicMock()
        mock_svc.name = "management"
        mock_svc.url = ":8042"
        mock_svc.status = "healthy"
        mock_svc.response_time_ms = 5.0
        mock_svc.version = None
        mock_result.services = {"management": mock_svc}

        with patch("fraisier.health_check.AggregateHealthChecker") as mock_checker_cls:
            mock_checker_cls.return_value.check_all.return_value = mock_result
            result = runner.invoke(main, ["-c", config_path, "health"])

        assert result.exit_code == 0
        assert "healthy" in result.output.lower()

    def test_health_json_output(self, tmp_path):
        from fraisier.cli import main

        runner = CliRunner()
        config_path = self._make_config(tmp_path)

        from fraisier.health_check import (
            AggregateHealthResult,
            ServiceHealthResult,
        )

        svc = ServiceHealthResult(
            name="management",
            url=":8042",
            status="healthy",
            response_time_ms=5.0,
            version="1.0.0",
        )
        agg = AggregateHealthResult(
            status="healthy",
            services={"management": svc},
            response_time_ms=12.0,
        )

        with patch("fraisier.health_check.AggregateHealthChecker") as mock_checker_cls:
            mock_checker_cls.return_value.check_all.return_value = agg
            result = runner.invoke(main, ["-c", config_path, "health", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "healthy"
        assert "management" in data["services"]

    def test_health_env_filter(self, tmp_path):
        from fraisier.cli import main

        runner = CliRunner()
        config_path = self._make_config(tmp_path)

        mock_result = MagicMock()
        mock_result.status = "healthy"
        mock_result.response_time_ms = 5.0
        mock_result.services = {}

        with patch("fraisier.health_check.AggregateHealthChecker") as mock_checker_cls:
            mock_checker_cls.return_value.check_all.return_value = mock_result
            result = runner.invoke(
                main, ["-c", config_path, "health", "--env", "development"]
            )

        assert result.exit_code == 0


class TestValidationCheck:
    """Test individual validation checks."""

    def test_check_result_pass(self):
        from fraisier.validation import ValidationCheckResult

        r = ValidationCheckResult(name="config_valid", passed=True)
        assert r.passed is True
        assert r.message is None

    def test_check_result_fail_with_message(self):
        from fraisier.validation import ValidationCheckResult

        r = ValidationCheckResult(
            name="provider_available",
            passed=False,
            message="Provider 'docker_compose' not installed",
        )
        assert r.passed is False
        assert "not installed" in r.message


class TestValidationRunner:
    """Test the ValidationRunner check registry and execution."""

    def test_config_valid_check_passes(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
fraises:
  my_api:
    type: api
    description: Test
    environments:
      development:
        app_path: /tmp/test
"""
        )
        from fraisier.config import FraisierConfig
        from fraisier.validation import ValidationRunner

        cfg = FraisierConfig(str(config_file))
        runner = ValidationRunner(cfg)
        results = runner.run_all()
        config_check = next((r for r in results if r.name == "config_valid"), None)
        assert config_check is not None
        assert config_check.passed is True

    def test_deploy_user_check(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
fraises:
  my_api:
    type: api
    description: Test
    environments:
      development:
        app_path: /tmp/test

deployment:
  deploy_user: nonexistent_user_xyz_12345
"""
        )
        from fraisier.config import FraisierConfig
        from fraisier.validation import ValidationRunner

        cfg = FraisierConfig(str(config_file))
        runner = ValidationRunner(cfg)
        results = runner.run_all()
        user_check = next((r for r in results if r.name == "deploy_user"), None)
        assert user_check is not None
        assert user_check.passed is False

    def test_all_checks_return_results(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
fraises:
  my_api:
    type: api
    description: Test
    environments:
      development:
        app_path: /tmp/test
"""
        )
        from fraisier.config import FraisierConfig
        from fraisier.validation import ValidationRunner

        cfg = FraisierConfig(str(config_file))
        runner = ValidationRunner(cfg)
        results = runner.run_all()
        assert len(results) >= 2
        assert all(hasattr(r, "passed") for r in results)

    def test_overall_pass(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
fraises:
  my_api:
    type: api
    description: Test
    environments:
      development:
        app_path: /tmp/test
"""
        )
        from fraisier.config import FraisierConfig
        from fraisier.validation import ValidationRunner

        cfg = FraisierConfig(str(config_file))
        runner = ValidationRunner(cfg)
        results = runner.run_all()
        # config_valid should pass, deploy_user may fail depending on env
        assert any(r.passed for r in results)


class TestValidateCLI:
    """Test fraisier validate CLI command."""

    def test_validate_command_exists(self, tmp_path):
        from fraisier.cli import main

        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
fraises:
  my_api:
    type: api
    description: Test
    environments:
      development:
        app_path: /tmp/test
"""
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(config_file), "validate", "--help"])
        assert result.exit_code == 0
        assert "validate" in result.output.lower()

    def test_validate_runs_checks(self, tmp_path):
        from fraisier.cli import main

        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
fraises:
  my_api:
    type: api
    description: Test
    environments:
      development:
        app_path: /tmp/test
"""
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(config_file), "validate"])
        # Should run without crashing, exit code depends on check results
        assert result.exit_code in (0, 1)
        # Output should contain check names or severity labels
        output_lower = result.output.lower()
        has_output = "pass" in output_lower or "error" in output_lower
        assert has_output or "warn" in output_lower

    def test_validate_json_output(self, tmp_path):
        from fraisier.cli import main

        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
fraises:
  my_api:
    type: api
    description: Test
    environments:
      development:
        app_path: /tmp/test
"""
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", str(config_file), "validate", "--json"])
        assert result.exit_code in (0, 1)
        data = json.loads(result.output, strict=False)
        assert "checks" in data
        assert "passed" in data


class TestDriftDetection:
    """Test scaffold drift detection — detect modified generated files."""

    def test_no_drift_when_files_match(self, tmp_path):
        from fraisier.validation import detect_drift

        output_dir = tmp_path / "generated"
        output_dir.mkdir()
        (output_dir / "install.sh").write_text("#!/bin/bash\necho hello\n")

        template_hashes = {
            "install.sh": _hash_content("#!/bin/bash\necho hello\n"),
        }

        results = detect_drift(output_dir, template_hashes)
        assert len(results) == 0

    def test_drift_detected_when_file_modified(self, tmp_path):
        from fraisier.validation import detect_drift

        output_dir = tmp_path / "generated"
        output_dir.mkdir()
        (output_dir / "install.sh").write_text("#!/bin/bash\necho modified\n")

        template_hashes = {
            "install.sh": _hash_content("#!/bin/bash\necho hello\n"),
        }

        results = detect_drift(output_dir, template_hashes)
        assert len(results) == 1
        assert results[0].name == "install.sh"
        assert results[0].drifted is True

    def test_drift_missing_file(self, tmp_path):
        from fraisier.validation import detect_drift

        output_dir = tmp_path / "generated"
        output_dir.mkdir()

        template_hashes = {
            "install.sh": "sha256:abc",
        }

        results = detect_drift(output_dir, template_hashes)
        assert len(results) == 1
        assert results[0].drifted is True
        assert "missing" in results[0].message.lower()

    def test_opt_out_per_file(self, tmp_path):
        from fraisier.validation import detect_drift

        output_dir = tmp_path / "generated"
        output_dir.mkdir()
        (output_dir / "custom.conf").write_text("modified content")

        template_hashes = {
            "custom.conf": _hash_content("original content"),
        }

        results = detect_drift(output_dir, template_hashes, ignore={"custom.conf"})
        assert len(results) == 0

    def test_multiple_files_mixed_drift(self, tmp_path):
        from fraisier.validation import detect_drift

        output_dir = tmp_path / "generated"
        output_dir.mkdir()
        (output_dir / "a.sh").write_text("original")
        (output_dir / "b.sh").write_text("changed")

        template_hashes = {
            "a.sh": _hash_content("original"),
            "b.sh": _hash_content("not changed"),
        }

        results = detect_drift(output_dir, template_hashes)
        assert len(results) == 1
        assert results[0].name == "b.sh"


def _hash_content(content: str) -> str:
    """Helper to compute sha256 hash matching detect_drift implementation."""
    import hashlib

    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()

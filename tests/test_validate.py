"""Tests for fraisier validate improvements."""

import json

import yaml
from click.testing import CliRunner

from fraisier.cli import main


def _write_config(tmp_path, config_dict):
    """Write a fraises.yaml and return the path."""
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(yaml.dump(config_dict))
    return str(cfg)


class TestValidateMissingFields:
    """Test validation catches missing required fields."""

    def test_missing_type_field(self, tmp_path):
        """Test validation catches fraise missing 'type' field."""
        cfg = _write_config(
            tmp_path,
            {
                "fraises": {
                    "my_app": {
                        "environments": {
                            "production": {"name": "app", "app_path": "/var/www/app"},
                        },
                    },
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", cfg, "validate", "--json"])
        data = json.loads(result.output, strict=False)
        checks = data["checks"]
        # Should have a failing check about missing type
        field_checks = [c for c in checks if "required" in c["name"]]
        assert any(not c["passed"] for c in field_checks)

    def test_missing_environments(self, tmp_path):
        """Test validation catches fraise with no environments."""
        cfg = _write_config(
            tmp_path,
            {
                "fraises": {
                    "my_app": {"type": "api"},
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", cfg, "validate", "--json"])
        data = json.loads(result.output, strict=False)
        checks = data["checks"]
        env_checks = [c for c in checks if "environment" in c["name"]]
        assert any(not c["passed"] for c in env_checks)

    def test_missing_app_path_in_production(self, tmp_path):
        """Test validation catches missing app_path in production env."""
        cfg = _write_config(
            tmp_path,
            {
                "fraises": {
                    "my_app": {
                        "type": "api",
                        "environments": {
                            "production": {"name": "app"},
                        },
                    },
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", cfg, "validate", "--json"])
        data = json.loads(result.output, strict=False)
        checks = data["checks"]
        field_checks = [c for c in checks if "required" in c["name"]]
        assert any(not c["passed"] for c in field_checks)


class TestValidateHealthCheckURLs:
    """Test validation catches invalid health check URLs."""

    def test_invalid_health_check_url(self, tmp_path):
        """Test validation catches malformed health check URL."""
        cfg = _write_config(
            tmp_path,
            {
                "fraises": {
                    "my_app": {
                        "type": "api",
                        "environments": {
                            "production": {
                                "name": "app",
                                "app_path": "/var/www/app",
                                "health_check": {"url": "not-a-url", "timeout": 30},
                            },
                        },
                    },
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", cfg, "validate", "--json"])
        data = json.loads(result.output, strict=False)
        checks = data["checks"]
        hc_checks = [c for c in checks if "health" in c["name"]]
        assert any(not c["passed"] for c in hc_checks)

    def test_valid_health_check_url_passes(self, tmp_path):
        """Test valid health check URL passes validation."""
        cfg = _write_config(
            tmp_path,
            {
                "fraises": {
                    "my_app": {
                        "type": "api",
                        "environments": {
                            "production": {
                                "name": "app",
                                "app_path": "/var/www/app",
                                "health_check": {
                                    "url": "http://localhost:8000/health",
                                    "timeout": 30,
                                },
                            },
                        },
                    },
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", cfg, "validate", "--json"])
        data = json.loads(result.output, strict=False)
        checks = data["checks"]
        hc_checks = [c for c in checks if "health" in c["name"]]
        assert all(c["passed"] for c in hc_checks)


class TestValidateUnknownStrategy:
    """Test validation catches unknown migration strategy."""

    def test_unknown_database_strategy(self, tmp_path):
        """Test validation catches unknown database strategy."""
        cfg = _write_config(
            tmp_path,
            {
                "fraises": {
                    "my_app": {
                        "type": "api",
                        "environments": {
                            "production": {
                                "name": "app",
                                "app_path": "/var/www/app",
                                "database": {
                                    "name": "mydb",
                                    "strategy": "yolo_deploy",
                                },
                            },
                        },
                    },
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", cfg, "validate", "--json"])
        data = json.loads(result.output, strict=False)
        checks = data["checks"]
        strat_checks = [c for c in checks if "strategy" in c["name"]]
        assert any(not c["passed"] for c in strat_checks)

    def test_valid_strategy_passes(self, tmp_path):
        """Test known strategies pass validation."""
        cfg = _write_config(
            tmp_path,
            {
                "fraises": {
                    "my_app": {
                        "type": "api",
                        "environments": {
                            "production": {
                                "name": "app",
                                "app_path": "/var/www/app",
                                "database": {
                                    "name": "mydb",
                                    "strategy": "migrate",
                                },
                            },
                        },
                    },
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", cfg, "validate", "--json"])
        data = json.loads(result.output, strict=False)
        checks = data["checks"]
        strat_checks = [c for c in checks if "strategy" in c["name"]]
        assert all(c["passed"] for c in strat_checks)


class TestValidateFixSuggestions:
    """Test that error messages include fix suggestions."""

    def test_fix_suggestion_for_missing_type(self, tmp_path):
        """Test fix suggestion appears for missing type field."""
        cfg = _write_config(
            tmp_path,
            {
                "fraises": {
                    "my_app": {
                        "environments": {
                            "production": {"name": "app", "app_path": "/x"},
                        },
                    },
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", cfg, "validate", "--json"])
        data = json.loads(result.output, strict=False)
        checks = data["checks"]
        failing = [c for c in checks if not c["passed"] and c.get("message")]
        assert any("fix" in c["message"].lower() for c in failing)

    def test_fix_suggestion_for_invalid_url(self, tmp_path):
        """Test fix suggestion for invalid health check URL."""
        cfg = _write_config(
            tmp_path,
            {
                "fraises": {
                    "my_app": {
                        "type": "api",
                        "environments": {
                            "production": {
                                "name": "app",
                                "app_path": "/x",
                                "health_check": {"url": "bad"},
                            },
                        },
                    },
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", cfg, "validate", "--json"])
        data = json.loads(result.output, strict=False)
        checks = data["checks"]
        failing = [c for c in checks if not c["passed"] and c.get("message")]
        assert any("fix" in c["message"].lower() for c in failing)


class TestValidateSeverity:
    """Test validation groups results by severity."""

    def test_results_have_severity_field(self, tmp_path):
        """Test validation results include severity (error/warning)."""
        cfg = _write_config(
            tmp_path,
            {
                "fraises": {
                    "my_app": {
                        "type": "api",
                        "environments": {
                            "production": {"name": "app", "app_path": "/x"},
                        },
                    },
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", cfg, "validate", "--json"])
        data = json.loads(result.output, strict=False)
        checks = data["checks"]
        assert all("severity" in c for c in checks)

    def test_missing_required_is_error_severity(self, tmp_path):
        """Test missing required field is severity 'error'."""
        cfg = _write_config(
            tmp_path,
            {
                "fraises": {
                    "my_app": {
                        "environments": {
                            "production": {"name": "app"},
                        },
                    },
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", cfg, "validate", "--json"])
        data = json.loads(result.output, strict=False)
        checks = data["checks"]
        failing = [c for c in checks if not c["passed"]]
        assert any(c.get("severity") == "error" for c in failing)

    def test_missing_health_check_is_warning(self, tmp_path):
        """Test missing health check is severity 'warning'."""
        cfg = _write_config(
            tmp_path,
            {
                "fraises": {
                    "my_app": {
                        "type": "api",
                        "environments": {
                            "production": {"name": "app", "app_path": "/x"},
                        },
                    },
                },
            },
        )
        runner = CliRunner()
        result = runner.invoke(main, ["-c", cfg, "validate", "--json"])
        data = json.loads(result.output, strict=False)
        checks = data["checks"]
        warnings = [c for c in checks if c.get("severity") == "warning"]
        # A missing health check should generate a warning
        assert any("health" in c["name"] for c in warnings)

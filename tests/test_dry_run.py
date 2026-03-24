"""Tests for fraisier deploy --dry-run."""

import yaml
from click.testing import CliRunner

from fraisier.cli import main


def _write_config(tmp_path, config_dict):
    """Write a fraises.yaml and return the path."""
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(yaml.dump(config_dict))
    return str(cfg)


_FULL_CONFIG = {
    "fraises": {
        "my_api": {
            "type": "api",
            "description": "My API",
            "environments": {
                "production": {
                    "name": "my-api-prod",
                    "branch": "main",
                    "app_path": "/var/www/my-api",
                    "systemd_service": "gunicorn-myapi.service",
                    "database": {
                        "name": "myapp_production",
                        "strategy": "migrate",
                        "backup_before_deploy": True,
                    },
                    "health_check": {
                        "url": "http://localhost:8000/health",
                        "timeout": 30,
                    },
                },
            },
        },
    },
    "environments": {
        "production": {"server": "prod.example.com"},
    },
}


class TestDryRunOutput:
    """Test --dry-run shows the deployment plan."""

    def test_dry_run_exits_zero(self, tmp_path):
        """Test dry-run exits successfully."""
        cfg = _write_config(tmp_path, _FULL_CONFIG)
        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", cfg, "deploy", "my_api", "production", "--dry-run"]
        )
        assert result.exit_code == 0

    def test_dry_run_shows_fraise_and_environment(self, tmp_path):
        """Test dry-run output mentions fraise and environment."""
        cfg = _write_config(tmp_path, _FULL_CONFIG)
        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", cfg, "deploy", "my_api", "production", "--dry-run"]
        )
        assert "my_api" in result.output
        assert "production" in result.output

    def test_dry_run_shows_backup_plan(self, tmp_path):
        """Test dry-run shows what backup will be taken."""
        cfg = _write_config(tmp_path, _FULL_CONFIG)
        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", cfg, "deploy", "my_api", "production", "--dry-run"]
        )
        output = result.output.lower()
        assert "backup" in output
        assert "myapp_production" in output

    def test_dry_run_shows_migration_plan(self, tmp_path):
        """Test dry-run shows what migration will run."""
        cfg = _write_config(tmp_path, _FULL_CONFIG)
        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", cfg, "deploy", "my_api", "production", "--dry-run"]
        )
        output = result.output.lower()
        assert "migrat" in output
        assert "migrate" in output

    def test_dry_run_shows_service_restart(self, tmp_path):
        """Test dry-run shows what service will restart."""
        cfg = _write_config(tmp_path, _FULL_CONFIG)
        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", cfg, "deploy", "my_api", "production", "--dry-run"]
        )
        assert "gunicorn-myapi" in result.output

    def test_dry_run_shows_health_check(self, tmp_path):
        """Test dry-run shows what health check will verify."""
        cfg = _write_config(tmp_path, _FULL_CONFIG)
        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", cfg, "deploy", "my_api", "production", "--dry-run"]
        )
        assert "http://localhost:8000/health" in result.output

    def test_dry_run_shows_strategy(self, tmp_path):
        """Test dry-run shows the deployment strategy."""
        cfg = _write_config(tmp_path, _FULL_CONFIG)
        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", cfg, "deploy", "my_api", "production", "--dry-run"]
        )
        output = result.output.lower()
        assert "strategy" in output


class TestDryRunWithoutOptionalFields:
    """Test dry-run handles missing optional config gracefully."""

    def test_no_database_skips_backup_and_migration(self, tmp_path):
        """Test dry-run with no database doesn't mention backup/migration."""
        config = {
            "fraises": {
                "my_api": {
                    "type": "api",
                    "environments": {
                        "production": {
                            "name": "my-api",
                            "app_path": "/var/www/my-api",
                            "systemd_service": "myapi.service",
                        },
                    },
                },
            },
        }
        cfg = _write_config(tmp_path, config)
        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", cfg, "deploy", "my_api", "production", "--dry-run"]
        )
        assert result.exit_code == 0
        output = result.output.lower()
        # Should indicate no database/migration step
        assert "no database" in output or "skip" in output or "none" in output

    def test_no_health_check_indicates_skipped(self, tmp_path):
        """Test dry-run with no health check shows it will be skipped."""
        config = {
            "fraises": {
                "my_api": {
                    "type": "api",
                    "environments": {
                        "production": {
                            "name": "my-api",
                            "app_path": "/var/www/my-api",
                            "systemd_service": "myapi.service",
                        },
                    },
                },
            },
        }
        cfg = _write_config(tmp_path, config)
        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", cfg, "deploy", "my_api", "production", "--dry-run"]
        )
        assert result.exit_code == 0
        output = result.output.lower()
        assert "health" in output

    def test_dry_run_does_not_execute(self, tmp_path):
        """Test dry-run never calls deployer.execute()."""
        cfg = _write_config(tmp_path, _FULL_CONFIG)
        runner = CliRunner()
        result = runner.invoke(
            main, ["-c", cfg, "deploy", "my_api", "production", "--dry-run"]
        )
        assert result.exit_code == 0
        # Should not say "Deploying" or "successful" (those come from real deploy)
        assert "Deploying" not in result.output
        assert "successful" not in result.output.lower()

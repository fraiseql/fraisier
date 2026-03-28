"""Tests for configuration loading."""

import pytest
import yaml

from fraisier.config import FraisierConfig, get_config, reset_config


class TestFraisierConfig:
    """Tests for FraisierConfig."""

    def test_load_config(self, tmp_path):
        """Test loading configuration from file."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  my_api:
    type: api
    description: Test API
    environments:
      production:
        app_path: /var/www/api
        systemd_service: api.service
"""
        )

        config = FraisierConfig(str(config_file))
        fraise = config.get_fraise("my_api")

        assert fraise is not None
        assert fraise["type"] == "api"
        assert fraise["description"] == "Test API"

    def test_get_fraise_returns_none_for_missing(self, sample_config):
        """Test getting missing fraise returns None."""
        fraise = sample_config.get_fraise("nonexistent")

        assert fraise is None

    def test_get_environment(self, sample_config):
        """Test getting environment configuration."""
        env = sample_config.get_environment("my_api", "production")

        assert env is not None
        assert env["app_path"] == "/tmp/test-api-prod"
        assert env["systemd_service"] == "test-api-prod.service"

    def test_get_environment_returns_none_for_missing(self, sample_config):
        """Test getting missing environment returns None."""
        env = sample_config.get_environment("my_api", "nonexistent")

        assert env is None

    def test_get_git_provider(self, sample_config):
        """Test getting git provider config."""
        provider_config = sample_config.get_git_provider_config()

        assert provider_config is not None
        assert provider_config["provider"] == "github"
        assert provider_config["github"]["webhook_secret"] == "test-secret"

    def test_list_fraises(self, sample_config):
        """Test listing all fraises."""
        fraises = sample_config.list_fraises()

        assert "my_api" in fraises
        assert "data_pipeline" in fraises
        assert "backup_job" in fraises

    def test_list_environments_for_fraise(self, sample_config):
        """Test listing environments for a fraise."""
        envs = sample_config.list_environments("my_api")

        assert "development" in envs
        assert "production" in envs

    def test_fraise_type_detection(self, sample_config):
        """Test detecting fraise type."""
        assert sample_config.get_fraise("my_api")["type"] == "api"
        assert sample_config.get_fraise("data_pipeline")["type"] == "etl"
        assert sample_config.get_fraise("backup_job")["type"] == "scheduled"

    def test_invalid_yaml_raises_error(self, tmp_path):
        """Test that invalid YAML raises error."""
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("invalid: yaml: content: [")

        with pytest.raises(yaml.YAMLError):
            FraisierConfig(str(config_file))

    def test_missing_config_file_raises_error(self):
        """Test that missing config file raises error."""
        with pytest.raises(FileNotFoundError):
            FraisierConfig("/nonexistent/fraises.yaml")


class TestConfigSingleton:
    """Tests for config singleton lifecycle."""

    def test_reset_config_clears_singleton(self, tmp_path):
        """reset_config() clears singleton so next get_config() re-reads."""
        cfg1 = tmp_path / "a.yaml"
        cfg1.write_text("fraises:\n  api_a:\n    type: api\n")
        cfg2 = tmp_path / "b.yaml"
        cfg2.write_text("fraises:\n  api_b:\n    type: api\n")

        c1 = get_config(str(cfg1))
        assert c1.get_fraise("api_a") is not None

        reset_config()

        c2 = get_config(str(cfg2))
        assert c2.get_fraise("api_b") is not None
        assert c2.get_fraise("api_a") is None

    def test_get_config_with_new_path_replaces_singleton(self, tmp_path):
        """Calling get_config(path) with a new path replaces the singleton."""
        cfg1 = tmp_path / "a.yaml"
        cfg1.write_text("fraises:\n  svc1:\n    type: api\n")
        cfg2 = tmp_path / "b.yaml"
        cfg2.write_text("fraises:\n  svc2:\n    type: api\n")

        c1 = get_config(str(cfg1))
        assert c1.get_fraise("svc1") is not None

        c2 = get_config(str(cfg2))
        assert c2.get_fraise("svc2") is not None

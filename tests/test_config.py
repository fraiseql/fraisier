"""Tests for configuration loading."""

import pytest
import yaml

from fraisier.config import FraisierConfig, get_config, reset_config
from fraisier.errors import ConfigurationError


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


class TestBranchMappingNormalization:
    """branch_mapping property normalizes to list-of-dicts."""

    def test_single_dict_normalized_to_list(self, tmp_path):
        """Single-dict syntax returns a one-element list."""
        cfg = tmp_path / "f.yaml"
        cfg.write_text("""
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
branch_mapping:
  main:
    fraise: my_api
    environment: production
""")
        config = FraisierConfig(str(cfg))
        mapping = config.branch_mapping["main"]
        assert isinstance(mapping, list)
        assert len(mapping) == 1
        assert mapping[0]["fraise"] == "my_api"

    def test_list_syntax_returned_as_is(self, tmp_path):
        """List syntax is returned unchanged."""
        cfg = tmp_path / "f.yaml"
        cfg.write_text("""
fraises:
  api_a:
    type: api
    environments:
      production:
        app_path: /a
  api_b:
    type: api
    environments:
      production:
        app_path: /b
branch_mapping:
  main:
    - fraise: api_a
      environment: production
    - fraise: api_b
      environment: production
""")
        config = FraisierConfig(str(cfg))
        mapping = config.branch_mapping["main"]
        assert isinstance(mapping, list)
        assert len(mapping) == 2

    def test_mixed_syntax_all_normalized(self, tmp_path):
        """Some branches single-dict, others list — all become lists."""
        cfg = tmp_path / "f.yaml"
        cfg.write_text("""
fraises:
  api_a:
    type: api
    environments:
      production:
        app_path: /a
      staging:
        app_path: /a-stg
  api_b:
    type: api
    environments:
      production:
        app_path: /b
branch_mapping:
  main:
    - fraise: api_a
      environment: production
    - fraise: api_b
      environment: production
  develop:
    fraise: api_a
    environment: staging
""")
        config = FraisierConfig(str(cfg))
        assert isinstance(config.branch_mapping["main"], list)
        assert isinstance(config.branch_mapping["develop"], list)
        assert len(config.branch_mapping["develop"]) == 1


class TestBranchMappingValidation:
    """Validation for branch_mapping entries."""

    def _make_config(self, tmp_path, branch_mapping_yaml, fraises_yaml=None):
        cfg = tmp_path / "f.yaml"
        fraises = (
            fraises_yaml
            or """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
"""
        )
        cfg.write_text(f"{fraises}\n{branch_mapping_yaml}")
        return FraisierConfig(str(cfg))

    def test_list_entry_missing_fraise_key(self, tmp_path):
        """List entry without 'fraise' key raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match="fraise"):
            self._make_config(
                tmp_path,
                """
branch_mapping:
  main:
    - environment: production
""",
            )

    def test_list_entry_missing_environment_key(self, tmp_path):
        """List entry without 'environment' key raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match="environment"):
            self._make_config(
                tmp_path,
                """
branch_mapping:
  main:
    - fraise: my_api
""",
            )

    def test_duplicate_fraise_environment_pair(self, tmp_path):
        """Duplicate (fraise, environment) in same branch raises error."""
        with pytest.raises(ConfigurationError, match="duplicate"):
            self._make_config(
                tmp_path,
                """
branch_mapping:
  main:
    - fraise: my_api
      environment: production
    - fraise: my_api
      environment: production
""",
            )

    def test_nonexistent_fraise_raises(self, tmp_path):
        """Reference to non-existent fraise raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match="nonexistent"):
            self._make_config(
                tmp_path,
                """
branch_mapping:
  main:
    - fraise: nonexistent
      environment: production
""",
            )

    def test_nonexistent_environment_raises(self, tmp_path):
        """Reference to non-existent environment raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match="staging"):
            self._make_config(
                tmp_path,
                """
branch_mapping:
  main:
    - fraise: my_api
      environment: staging
""",
            )


class TestGetFraisesForBranch:
    """Tests for get_fraises_for_branch()."""

    def test_multi_fraise_branch_returns_all(self, tmp_path):
        """Branch with 3 fraises returns list of 3 configs."""
        cfg = tmp_path / "f.yaml"
        cfg.write_text("""
fraises:
  api_a:
    type: api
    environments:
      production:
        app_path: /a
  api_b:
    type: api
    environments:
      production:
        app_path: /b
  worker:
    type: etl
    environments:
      production:
        app_path: /w
branch_mapping:
  main:
    - fraise: api_a
      environment: production
    - fraise: api_b
      environment: production
    - fraise: worker
      environment: production
""")
        config = FraisierConfig(str(cfg))
        results = config.get_fraises_for_branch("main")
        assert len(results) == 3
        names = {r["fraise_name"] for r in results}
        assert names == {"api_a", "api_b", "worker"}

    def test_single_fraise_old_syntax(self, tmp_path):
        """Old single-dict syntax still returns list of 1."""
        cfg = tmp_path / "f.yaml"
        cfg.write_text("""
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
branch_mapping:
  main:
    fraise: my_api
    environment: production
""")
        config = FraisierConfig(str(cfg))
        results = config.get_fraises_for_branch("main")
        assert len(results) == 1
        assert results[0]["fraise_name"] == "my_api"

    def test_unknown_branch_returns_empty(self, tmp_path):
        """Unknown branch returns empty list."""
        cfg = tmp_path / "f.yaml"
        cfg.write_text("fraises:\n  x:\n    type: api\n")
        config = FraisierConfig(str(cfg))
        assert config.get_fraises_for_branch("nonexistent") == []

    def test_deprecated_get_fraise_for_branch_returns_first(self, tmp_path):
        """Deprecated method returns first match."""
        cfg = tmp_path / "f.yaml"
        cfg.write_text("""
fraises:
  api_a:
    type: api
    environments:
      production:
        app_path: /a
  api_b:
    type: api
    environments:
      production:
        app_path: /b
branch_mapping:
  main:
    - fraise: api_a
      environment: production
    - fraise: api_b
      environment: production
""")
        config = FraisierConfig(str(cfg))
        result = config.get_fraise_for_branch("main")
        assert result is not None
        assert result["fraise_name"] == "api_a"


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

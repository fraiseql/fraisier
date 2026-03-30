"""Tests for configuration loading."""

import pytest
import yaml

from fraisier.config import FraisierConfig, get_config, reset_config
from fraisier.errors import ConfigurationError, ValidationError


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


class TestFraisierConfigEnvVar:
    """FRAISIER_CONFIG environment variable support."""

    def test_resolves_from_env_var(self, tmp_path, monkeypatch):
        """FRAISIER_CONFIG env var is used when no explicit path given."""
        cfg = tmp_path / "custom.yaml"
        cfg.write_text("fraises:\n  env_api:\n    type: api\n")
        monkeypatch.setenv("FRAISIER_CONFIG", str(cfg))

        config = FraisierConfig()
        assert config.get_fraise("env_api") is not None

    def test_explicit_path_takes_precedence_over_env(self, tmp_path, monkeypatch):
        """Explicit config_path takes precedence over FRAISIER_CONFIG."""
        env_cfg = tmp_path / "env.yaml"
        env_cfg.write_text("fraises:\n  env_svc:\n    type: api\n")
        explicit_cfg = tmp_path / "explicit.yaml"
        explicit_cfg.write_text("fraises:\n  explicit_svc:\n    type: api\n")

        monkeypatch.setenv("FRAISIER_CONFIG", str(env_cfg))
        config = FraisierConfig(str(explicit_cfg))
        assert config.get_fraise("explicit_svc") is not None
        assert config.get_fraise("env_svc") is None


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


class TestGetEnvironmentsForServer:
    """Tests for FraisierConfig.get_environments_for_server."""

    def _make_config(self, tmp_path, yaml_content: str) -> FraisierConfig:
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(str(p))

    CONFIG_WITH_SERVERS = """\
fraises:
  my_api:
    type: api
    description: Test
    environments:
      development:
        app_path: /var/www/dev
      production:
        app_path: /var/www/prod

environments:
  development:
    server: dev.example.io
  staging:
    server: dev.example.io
  production:
    server: prod.example.io
"""

    def test_returns_matching_environments(self, tmp_path):
        config = self._make_config(tmp_path, self.CONFIG_WITH_SERVERS)
        result = config.get_environments_for_server("dev.example.io")
        assert sorted(result) == ["development", "staging"]

    def test_returns_single_match(self, tmp_path):
        config = self._make_config(tmp_path, self.CONFIG_WITH_SERVERS)
        result = config.get_environments_for_server("prod.example.io")
        assert result == ["production"]

    def test_returns_empty_for_unknown_server(self, tmp_path):
        config = self._make_config(tmp_path, self.CONFIG_WITH_SERVERS)
        result = config.get_environments_for_server("unknown.example.io")
        assert result == []

    def test_returns_empty_when_no_global_environments(self, tmp_path):
        config = self._make_config(
            tmp_path,
            "fraises:\n  svc:\n    type: api\n",
        )
        result = config.get_environments_for_server("any.host")
        assert result == []


class TestRestoreMigrateValidation:
    """Validation for restore_migrate strategy config."""

    def _make_config(self, tmp_path, yaml_content: str) -> FraisierConfig:
        p = tmp_path / "fraises.yaml"
        p.write_text(yaml_content)
        return FraisierConfig(str(p))

    def test_restore_migrate_requires_backup_dir(self, tmp_path):
        with pytest.raises(ValidationError, match="backup_dir"):
            self._make_config(
                tmp_path,
                """\
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /var/www/staging
        database:
          name: staging_db
          strategy: restore_migrate
""",
            )

    def test_restore_migrate_requires_db_name(self, tmp_path):
        with pytest.raises(ValidationError, match=r"database\.name"):
            self._make_config(
                tmp_path,
                """\
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /var/www/staging
        database:
          strategy: restore_migrate
          restore:
            backup_dir: /backup/production
""",
            )

    def test_restore_migrate_valid_config_passes(self, tmp_path):
        config = self._make_config(
            tmp_path,
            """\
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /var/www/staging
        database:
          name: staging_db
          strategy: restore_migrate
          restore:
            backup_dir: /backup/production
            backup_pattern: "*.dump"
            max_age_hours: 48
            target_owner: app_user
            create_template: true
            template_name: staging_template
            min_tables: 300
""",
        )
        env = config.get_fraise_environment("my_api", "staging")
        assert env is not None
        assert env["database"]["strategy"] == "restore_migrate"
        assert env["database"]["restore"]["backup_dir"] == "/backup/production"


class TestConfigDefaultLocations:
    """Config resolution should prefer CWD over /opt/fraisier/ (#26)."""

    def test_cwd_takes_priority_over_opt(self, tmp_path, monkeypatch):
        """fraises.yaml in CWD is found before /opt/fraisier/fraises.yaml."""
        cwd_config = tmp_path / "fraises.yaml"
        cwd_config.write_text(
            "git:\n  provider: github\n  github:\n    webhook_secret: s\nfraises: {}\n"
        )
        monkeypatch.chdir(tmp_path)

        config = FraisierConfig()
        assert config.config_path == cwd_config


class TestGetDeployUser:
    """get_deploy_user() resolves per-env override vs scaffold default (#28)."""

    def test_returns_scaffold_default_when_no_override(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            "git:\n  provider: github\n  github:\n    webhook_secret: s\n"
            "scaffold:\n  deploy_user: fraisier\n"
            "fraises:\n  my_api:\n    type: api\n"
            "    environments:\n      production:\n"
            "        app_path: /var/www/api\n"
        )
        config = FraisierConfig(str(config_file))
        assert config.get_deploy_user("my_api", "production") == "fraisier"

    def test_returns_env_override_when_set(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            "git:\n  provider: github\n  github:\n    webhook_secret: s\n"
            "scaffold:\n  deploy_user: fraisier\n"
            "fraises:\n  my_api:\n    type: api\n"
            "    environments:\n      production:\n"
            "        app_path: /var/www/api\n"
            "        deploy_user: prod-deployer\n"
        )
        config = FraisierConfig(str(config_file))
        assert config.get_deploy_user("my_api", "production") == "prod-deployer"

    def test_top_level_deploy_user_used_when_no_scaffold(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            "git:\n  provider: github\n  github:\n    webhook_secret: s\n"
            "deploy_user: top-level-deployer\n"
            "fraises:\n  my_api:\n    type: api\n"
            "    environments:\n      production:\n"
            "        app_path: /var/www/api\n"
        )
        config = FraisierConfig(str(config_file))
        assert config.scaffold.deploy_user == "top-level-deployer"
        assert config.get_deploy_user("my_api", "production") == "top-level-deployer"

    def test_scaffold_deploy_user_takes_priority_over_top_level(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            "git:\n  provider: github\n  github:\n    webhook_secret: s\n"
            "deploy_user: top-level-deployer\n"
            "scaffold:\n  deploy_user: scaffold-deployer\n"
            "fraises:\n  my_api:\n    type: api\n"
            "    environments:\n      production:\n"
            "        app_path: /var/www/api\n"
        )
        config = FraisierConfig(str(config_file))
        assert config.scaffold.deploy_user == "scaffold-deployer"

    def test_different_envs_can_have_different_deploy_users(self, tmp_path):
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            "git:\n  provider: github\n  github:\n    webhook_secret: s\n"
            "scaffold:\n  deploy_user: fraisier\n"
            "fraises:\n  my_api:\n    type: api\n"
            "    environments:\n      development:\n"
            "        app_path: /var/www/dev\n"
            "      production:\n"
            "        app_path: /var/www/prod\n"
            "        deploy_user: prod-deployer\n"
        )
        config = FraisierConfig(str(config_file))
        assert config.get_deploy_user("my_api", "development") == "fraisier"
        assert config.get_deploy_user("my_api", "production") == "prod-deployer"

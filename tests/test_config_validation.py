"""Tests for config validation at load time."""

import pytest

from fraisier.config import FraisierConfig
from fraisier.errors import ValidationError


def _write_config(tmp_path, content):
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(content)
    return config_file


class TestConfigValidation:
    """Config values must be type-validated at load time."""

    def test_rejects_non_numeric_health_check_timeout(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        health_check:
          timeout: "hello"
""",
        )
        with pytest.raises(ValidationError, match=r"timeout.*must be.*number"):
            FraisierConfig(config_file)

    def test_rejects_non_numeric_retries(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        health_check:
          retries: "lots"
""",
        )
        with pytest.raises(ValidationError, match=r"retries.*must be.*number"):
            FraisierConfig(config_file)

    def test_rejects_missing_app_path_with_health_check(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        health_check:
          url: http://localhost:8000/health
""",
        )
        with pytest.raises(ValidationError, match=r"app_path.*required"):
            FraisierConfig(config_file)

    def test_accepts_valid_config(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        health_check:
          timeout: 30
          retries: 5
""",
        )
        config = FraisierConfig(config_file)
        assert config.get_fraise("my_api") is not None

    def test_rejects_invalid_clone_url(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        clone_url: "not a url"
""",
        )
        with pytest.raises(ValidationError, match=r"clone_url.*valid git URL"):
            FraisierConfig(config_file)

    def test_accepts_ssh_clone_url(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        clone_url: "git@github.com:org/repo.git"
""",
        )
        config = FraisierConfig(config_file)
        assert config.get_fraise("my_api") is not None

    def test_accepts_https_clone_url(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        clone_url: "https://github.com/org/repo.git"
""",
        )
        config = FraisierConfig(config_file)
        assert config.get_fraise("my_api") is not None

    def test_accepts_local_path_clone_url(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        clone_url: "/var/repos/myapi.git"
""",
        )
        config = FraisierConfig(config_file)
        assert config.get_fraise("my_api") is not None

    def test_rejects_non_string_database_url(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        database:
          strategy: migrate
          database_url: 12345
""",
        )
        with pytest.raises(ValidationError, match=r"database_url.*must be a string"):
            FraisierConfig(config_file)

    def test_rejects_invalid_database_url_scheme(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        database:
          strategy: migrate
          database_url: "mysql://localhost/mydb"
""",
        )
        with pytest.raises(ValidationError, match=r"database_url.*must start with"):
            FraisierConfig(config_file)

    def test_accepts_valid_database_url(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        database:
          strategy: migrate
          database_url: "postgresql:///mydb?host=/var/run/postgresql"
""",
        )
        config = FraisierConfig(config_file)
        assert config.get_fraise("my_api") is not None

    def test_accepts_postgres_scheme_database_url(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        database:
          strategy: migrate
          database_url: "postgres://localhost/mydb"
""",
        )
        config = FraisierConfig(config_file)
        assert config.get_fraise("my_api") is not None

    def test_rejects_unknown_strategy(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        database:
          strategy: canary
""",
        )
        with pytest.raises(ValidationError, match=r"strategy.*canary"):
            FraisierConfig(config_file)

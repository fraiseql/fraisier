"""Tests for config validation at load time."""

import pytest

from fraisier.config import FraisierConfig
from fraisier.errors import ValidationError
from tests._eager_load import eager_load


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
            eager_load(config_file)

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
            eager_load(config_file)

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
            eager_load(config_file)

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
            eager_load(config_file)

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
            eager_load(config_file)

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
        with pytest.raises(
            ValidationError, match=r"database\.database_url.*PostgreSQL URL"
        ):
            eager_load(config_file)

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

    def test_rejects_rebuild_without_admin_url(self, tmp_path):
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
          strategy: rebuild
          database_url: "postgresql:///mydb?host=/var/run/postgresql"
""",
        )
        with pytest.raises(
            ValidationError,
            match=r"strategy 'rebuild' requires database\.admin_url",
        ):
            eager_load(config_file)

    def test_rejects_restore_migrate_without_admin_url(self, tmp_path):
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
          name: mydb
          strategy: restore_migrate
          restore:
            backup_dir: /var/backups/fraisier
          database_url: "postgresql:///mydb?host=/var/run/postgresql"
""",
        )
        with pytest.raises(
            ValidationError,
            match=r"strategy 'restore_migrate' requires database\.admin_url",
        ):
            eager_load(config_file)

    def test_accepts_rebuild_with_admin_url(self, tmp_path):
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
          strategy: rebuild
          database_url: "postgresql:///mydb?host=/var/run/postgresql"
          admin_url: "postgresql:///postgres?host=/var/run/postgresql"
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
            eager_load(config_file)

    def test_accepts_valid_service_manager_systemd(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
service_manager: systemd
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
""",
        )
        config = FraisierConfig(config_file)
        assert config._config.get("service_manager") == "systemd"

    def test_accepts_valid_service_manager_rc(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
service_manager: rc
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
""",
        )
        config = FraisierConfig(config_file)
        assert config._config.get("service_manager") == "rc"

    def test_rejects_invalid_service_manager(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
service_manager: invalid
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
""",
        )
        with pytest.raises(ValidationError, match=r"Invalid service_manager"):
            eager_load(config_file)

    def test_accepts_restore_jobs(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /srv/myapi
        database:
          name: mydb
          strategy: restore_migrate
          admin_url: "postgresql:///postgres?host=/var/run/postgresql"
          database_url: "postgresql:///mydb?host=/var/run/postgresql"
          restore:
            backup_dir: /var/backups
            jobs: 4
""",
        )
        config = FraisierConfig(config_file)
        assert config.get_fraise("my_api") is not None

    def test_rejects_restore_jobs_zero(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /srv/myapi
        database:
          name: mydb
          strategy: restore_migrate
          admin_url: "postgresql:///postgres?host=/var/run/postgresql"
          database_url: "postgresql:///mydb?host=/var/run/postgresql"
          restore:
            backup_dir: /var/backups
            jobs: 0
""",
        )
        with pytest.raises(ValidationError, match=r"jobs.*must be.*positive"):
            eager_load(config_file)

    def test_rejects_restore_jobs_negative(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /srv/myapi
        database:
          name: mydb
          strategy: restore_migrate
          admin_url: "postgresql:///postgres?host=/var/run/postgresql"
          database_url: "postgresql:///mydb?host=/var/run/postgresql"
          restore:
            backup_dir: /var/backups
            jobs: -1
""",
        )
        with pytest.raises(ValidationError, match=r"jobs.*must be.*positive"):
            eager_load(config_file)

    def test_accepts_restore_preferred_compression(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /srv/myapi
        database:
          name: mydb
          strategy: restore_migrate
          admin_url: "postgresql:///postgres?host=/var/run/postgresql"
          database_url: "postgresql:///mydb?host=/var/run/postgresql"
          restore:
            backup_dir: /var/backups
            preferred_compression: lz4
""",
        )
        config = FraisierConfig(config_file)
        assert config.get_fraise("my_api") is not None

    def test_rejects_invalid_preferred_compression(self, tmp_path):
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      staging:
        app_path: /srv/myapi
        database:
          name: mydb
          strategy: restore_migrate
          admin_url: "postgresql:///postgres?host=/var/run/postgresql"
          database_url: "postgresql:///mydb?host=/var/run/postgresql"
          restore:
            backup_dir: /var/backups
            preferred_compression: brotli
""",
        )
        with pytest.raises(ValidationError, match=r"preferred_compression"):
            eager_load(config_file)


class TestPostMigrateValidation:
    """`database.post_migrate` schema validation (#204 PR A)."""

    _BASE_CONFIG = """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        database:
          name: mydb
          strategy: migrate
          database_url: "postgresql:///mydb?host=/var/run/postgresql"
          post_migrate: {entries}
"""

    def _write(self, tmp_path, entries: str):
        return _write_config(tmp_path, self._BASE_CONFIG.format(entries=entries))

    def test_accepts_entry_with_sql_dir_only(self, tmp_path):
        config_file = self._write(tmp_path, "[{sql_dir: db/7_grant/}]")
        FraisierConfig(config_file)  # must not raise

    def test_accepts_entry_with_sql_file_only(self, tmp_path):
        config_file = self._write(tmp_path, "[{sql_file: db/post_migrate.sql}]")
        FraisierConfig(config_file)

    def test_rejects_entry_with_both_sql_dir_and_sql_file(self, tmp_path):
        config_file = self._write(
            tmp_path,
            "[{sql_dir: db/7_grant/, sql_file: db/post_migrate.sql}]",
        )
        with pytest.raises(
            ValidationError, match=r"post_migrate.*either.*sql_dir.*or.*sql_file"
        ):
            eager_load(config_file)

    def test_rejects_entry_with_neither_sql_dir_nor_sql_file(self, tmp_path):
        config_file = self._write(tmp_path, "[{on_error: warn}]")
        with pytest.raises(
            ValidationError, match=r"post_migrate.*sql_dir.*or.*sql_file"
        ):
            eager_load(config_file)

    def test_on_error_defaults_to_halt(self, tmp_path):
        # No on_error key in the YAML — loader assigns "halt" at parse time.
        # Validation passes; default behaviour is checked in test_post_migrate.py.
        config_file = self._write(tmp_path, "[{sql_dir: db/7_grant/}]")
        FraisierConfig(config_file)

    def test_on_error_rejects_unknown_value(self, tmp_path):
        config_file = self._write(tmp_path, "[{sql_dir: db/7_grant/, on_error: panic}]")
        with pytest.raises(ValidationError, match=r"on_error.*'halt'.*'warn'"):
            eager_load(config_file)


class TestSmokeTestsValidation:
    """`smoke_tests` schema validation at config-load time (#204 PR B)."""

    _BASE_CONFIG = """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        health_check:
          url: https://api.example.com/health
        smoke_tests: {entries}
"""

    def _write(self, tmp_path, entries: str):
        return _write_config(tmp_path, self._BASE_CONFIG.format(entries=entries))

    def test_smoke_tests_happy_path(self, tmp_path):
        config_file = self._write(
            tmp_path,
            "[{name: me, method: GET, url: /me, "
            "assert: [{json_path: $.id, not_null: true}]}]",
        )
        FraisierConfig(config_file)  # must not raise

    def test_accepts_relative_url_when_health_check_configured(self, tmp_path):
        config_file = self._write(
            tmp_path, "[{name: t, method: GET, url: /me, assert: []}]"
        )
        FraisierConfig(config_file)

    def test_rejects_relative_url_when_health_check_not_configured(self, tmp_path):
        # Drop health_check from the base config.
        yaml = """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        smoke_tests:
          - {name: t, method: GET, url: /me, assert: []}
"""
        config_file = _write_config(tmp_path, yaml)
        with pytest.raises(
            ValidationError, match=r"smoke_tests.*relative.*health_check"
        ):
            eager_load(config_file)

    def test_accepts_absolute_url_without_health_check(self, tmp_path):
        yaml = """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        smoke_tests:
          - {name: t, method: GET, url: "https://other.example/me",
             assert: []}
"""
        config_file = _write_config(tmp_path, yaml)
        FraisierConfig(config_file)

    def test_rejects_unknown_method(self, tmp_path):
        config_file = self._write(
            tmp_path, "[{name: t, method: MERGE, url: /me, assert: []}]"
        )
        with pytest.raises(ValidationError, match=r"smoke_tests.*method"):
            eager_load(config_file)

    def test_rejects_unknown_assertion_key(self, tmp_path):
        config_file = self._write(
            tmp_path,
            "[{name: t, method: GET, url: /me, "
            "assert: [{json_path: $.x, regex: '^a$'}]}]",
        )
        with pytest.raises(
            ValidationError, match=r"smoke_tests.*unknown assertion key"
        ):
            eager_load(config_file)

    @pytest.mark.parametrize("bad_path", ["$..foo", "$.a[0]", "$.*", "$.@.x"])
    def test_rejects_unsupported_jsonpath_syntax(self, tmp_path, bad_path):
        config_file = self._write(
            tmp_path,
            "[{name: t, method: GET, url: /me, "
            f"assert: [{{json_path: '{bad_path}', not_null: true}}]}}]",
        )
        with pytest.raises(ValidationError, match=r"unsupported JSONPath"):
            eager_load(config_file)

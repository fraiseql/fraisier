"""Tests for fraisier.dbops._url — PostgreSQL URL rewriting."""

import pytest

from fraisier.dbops._url import replace_db_name


class TestReplaceDbName:
    """replace_db_name preserves URL structure for all connection styles."""

    def test_tcp_url(self):
        url = "postgresql://user:pass@localhost:5432/original_db"
        assert (
            replace_db_name(url, "new_db")
            == "postgresql://user:pass@localhost:5432/new_db"
        )

    def test_tcp_url_to_postgres(self):
        url = "postgresql://user:pass@localhost:5432/myapp"
        assert (
            replace_db_name(url, "postgres")
            == "postgresql://user:pass@localhost:5432/postgres"
        )

    def test_unix_socket_url(self):
        url = "postgresql:///postgres?host=/var/run/postgresql"
        assert (
            replace_db_name(url, "myapp_db")
            == "postgresql:///myapp_db?host=/var/run/postgresql"
        )

    def test_unix_socket_url_to_postgres(self):
        url = "postgresql:///myapp_db?host=/var/run/postgresql"
        assert (
            replace_db_name(url, "postgres")
            == "postgresql:///postgres?host=/var/run/postgresql"
        )

    def test_unix_socket_no_query(self):
        url = "postgresql:///mydb"
        assert replace_db_name(url, "other_db") == "postgresql:///other_db"

    def test_tcp_url_with_query(self):
        url = "postgresql://user@host/db?sslmode=require"
        assert (
            replace_db_name(url, "new_db")
            == "postgresql://user@host/new_db?sslmode=require"
        )

    def test_preserves_fragment(self):
        url = "postgresql://user@host/db#fragment"
        assert (
            replace_db_name(url, "new_db") == "postgresql://user@host/new_db#fragment"
        )

    def test_a_dbname_parameter_is_dropped(self):
        """libpq reads ``?dbname=`` after the path, so leaving it would undo
        the replacement (#493)."""
        url = "postgresql:///db?host=/run/postgresql&dbname=db&sslmode=require"
        assert replace_db_name(url, "new_db") == (
            "postgresql:///new_db?host=/run/postgresql&sslmode=require"
        )

    def test_an_encoded_dbname_parameter_is_dropped(self):
        assert replace_db_name("postgresql://h/db?db%6Eame=db", "new") == (
            "postgresql://h/new"
        )


class TestResolveDbUrlRefusesEarly:
    """A URL fraisier cannot carry is refused at strategy entry (#493).

    Not at the first child: in the dump restore, the service is stopped
    before the first ``_pg_cmd`` on ``admin_url``.
    """

    def test_an_envvar_url_libpq_would_read_differently_is_refused(self, monkeypatch):
        from fraisier.config._lazy_env import LazyEnv
        from fraisier.dbops._url import resolve_db_url

        monkeypatch.setenv(
            "FRAISIER_TEST_DB_URL", "postgresql://u:hunter2@h/db?keepalives=1"
        )

        with pytest.raises(ValueError, match=r"admin_url.*keepalives") as exc:
            resolve_db_url(LazyEnv("FRAISIER_TEST_DB_URL"), role="admin_url")
        assert "hunter2" not in str(exc.value)

    def test_a_good_url_resolves_unchanged(self, monkeypatch):
        from fraisier.config._lazy_env import LazyEnv
        from fraisier.dbops._url import resolve_db_url

        monkeypatch.setenv("FRAISIER_TEST_DB_URL", "postgresql://u:p%40ss@h/db")

        assert resolve_db_url(LazyEnv("FRAISIER_TEST_DB_URL")) == (
            "postgresql://u:p%40ss@h/db"
        )


class TestValidationReportsTheRefusal:
    def test_a_literal_url_appears_in_validate_errors(self):
        from fraisier.config._validation import validate_pg_url_string

        errors = validate_pg_url_string(
            "api", "database_url", "postgresql:///db?bogus=1"
        )

        assert len(errors) == 1
        assert "api: database.database_url" in errors[0]
        assert "bogus" in errors[0]

    @pytest.mark.parametrize(
        "url", ["mysql://u:hunter2@h/db", "postgresql://u:hunter2@h/db?bogus=1"]
    )
    def test_the_error_never_echoes_the_password(self, url):
        from fraisier.config._validation import validate_pg_url_string

        [error] = validate_pg_url_string("api", "database_url", url)

        assert "hunter2" not in error


def test_the_restore_strategy_refuses_a_bad_admin_url_before_anything_runs():
    from pathlib import Path
    from unittest.mock import MagicMock

    from fraisier.strategies._restore import RestoreConfig, RestoreMigrateStrategy

    service_manager = MagicMock()
    with pytest.raises(ValueError, match="bogus"):
        RestoreMigrateStrategy(
            RestoreConfig(db_name="staging", backup_dir=Path("/b")),
            admin_url="postgresql:///postgres?bogus=1",
            service_manager=service_manager,
            service_name="api",
        )
    service_manager.assert_not_called()
    assert service_manager.method_calls == []

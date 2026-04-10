"""Tests for template-based database reset operations."""

import pytest

from fraisier.dbops.templates import (
    cleanup_templates,
    create_template,
    reset_from_template,
)

_TEST_URL = "postgresql://user:pass@localhost:5432/testdb"


class TestConnectionUrlPassthrough:
    """Verify connection_url is threaded to all underlying operations."""

    def test_create_template_passes_connection_url(self, monkeypatch):
        captured_urls: list[str] = []

        def fake_terminate(db_name, *, connection_url):
            captured_urls.append(connection_url)
            return (0, "", "")

        def fake_drop(db_name, *, force_disconnect=False, connection_url):
            captured_urls.append(connection_url)
            return (0, "", "")

        def fake_create(db_name, *, template=None, owner=None, connection_url):
            captured_urls.append(connection_url)
            return (0, "", "")

        monkeypatch.setattr(
            "fraisier.dbops.templates.terminate_backends", fake_terminate
        )
        monkeypatch.setattr("fraisier.dbops.templates.drop_db", fake_drop)
        monkeypatch.setattr("fraisier.dbops.templates.create_db", fake_create)

        create_template("mydb", connection_url=_TEST_URL)
        assert captured_urls
        assert all(u == _TEST_URL for u in captured_urls)

    def test_reset_from_template_passes_connection_url(self, monkeypatch):
        captured_urls: list[str] = []

        def fake_terminate(db_name, *, connection_url):
            captured_urls.append(connection_url)
            return (0, "", "")

        def fake_drop(db_name, *, force_disconnect=False, connection_url):
            captured_urls.append(connection_url)
            return (0, "", "")

        def fake_create(db_name, *, template=None, owner=None, connection_url):
            captured_urls.append(connection_url)
            return (0, "", "")

        monkeypatch.setattr(
            "fraisier.dbops.templates.terminate_backends", fake_terminate
        )
        monkeypatch.setattr("fraisier.dbops.templates.drop_db", fake_drop)
        monkeypatch.setattr("fraisier.dbops.templates.create_db", fake_create)

        reset_from_template("mydb", connection_url=_TEST_URL)
        assert captured_urls
        assert all(u == _TEST_URL for u in captured_urls)

    def test_cleanup_templates_passes_connection_url(self, monkeypatch):
        captured_urls: list[str] = []

        def fake_pg_cmd(cmd, *, connection_url):
            captured_urls.append(connection_url)
            return (0, "template_mydb\n", "")

        def fake_terminate(db_name, *, connection_url):
            captured_urls.append(connection_url)
            return (0, "", "")

        def fake_drop(db_name, *, force_disconnect=False, connection_url):
            captured_urls.append(connection_url)
            return (0, "", "")

        monkeypatch.setattr("fraisier.dbops.templates._pg_cmd", fake_pg_cmd)
        monkeypatch.setattr(
            "fraisier.dbops.templates.terminate_backends", fake_terminate
        )
        monkeypatch.setattr("fraisier.dbops.templates.drop_db", fake_drop)

        cleanup_templates("mydb", max_templates=0, connection_url=_TEST_URL)
        assert captured_urls
        assert all(u == _TEST_URL for u in captured_urls)


class TestCleanupTemplatesSQL:
    """Verify cleanup_templates uses parameterized SQL, not f-string interpolation."""

    def test_rejects_invalid_db_name(self):
        """db_name containing a single quote must be rejected by validation."""
        with pytest.raises(ValueError, match=r"Invalid.*database name"):
            cleanup_templates("foo'bar", connection_url=_TEST_URL)

    def test_uses_parameterized_query(self, monkeypatch):
        """SQL must use psql -v binding, not f-string interpolation."""
        captured_cmds: list[list[str]] = []

        def fake_pg_cmd(cmd: list[str], *, connection_url: str) -> tuple[int, str, str]:
            captured_cmds.append(cmd)
            return (0, "", "")

        monkeypatch.setattr("fraisier.dbops.templates._pg_cmd", fake_pg_cmd)
        cleanup_templates("mydb", connection_url=_TEST_URL)

        assert len(captured_cmds) >= 1
        sql_cmd = captured_cmds[0]
        # Must use -v for parameterization
        assert "-v" in sql_cmd
        # Must use psql bind variable syntax (:'varname')
        sql_str = " ".join(sql_cmd)
        assert ":'pattern'" in sql_str

    def test_parameterized_query_no_fstring_db_name(self, monkeypatch):
        """The SQL string itself must not contain the literal db_name."""
        captured_cmds: list[list[str]] = []

        def fake_pg_cmd(cmd: list[str], *, connection_url: str) -> tuple[int, str, str]:
            captured_cmds.append(cmd)
            return (0, "", "")

        monkeypatch.setattr("fraisier.dbops.templates._pg_cmd", fake_pg_cmd)
        cleanup_templates("mydb", connection_url=_TEST_URL)

        # Find the -c argument (the SQL string)
        sql_cmd = captured_cmds[0]
        c_index = sql_cmd.index("-c")
        sql_string = sql_cmd[c_index + 1]
        # SQL string must NOT contain the literal db name
        assert "mydb" not in sql_string

"""Tests for template-based database reset operations."""

import pytest

from fraisier.dbops.templates import cleanup_templates


class TestCleanupTemplatesSQL:
    """Verify cleanup_templates uses parameterized SQL, not f-string interpolation."""

    def test_rejects_invalid_db_name(self):
        """db_name containing a single quote must be rejected by validation."""
        with pytest.raises(ValueError, match=r"Invalid.*database name"):
            cleanup_templates("foo'bar")

    def test_uses_parameterized_query(self, monkeypatch):
        """SQL must use psql -v binding, not f-string interpolation."""
        captured_cmds: list[list[str]] = []

        def fake_pg_cmd(
            cmd: list[str], *, sudo_user: str = "postgres"
        ) -> tuple[int, str, str]:
            captured_cmds.append(cmd)
            return (0, "", "")

        monkeypatch.setattr("fraisier.dbops.templates._pg_cmd", fake_pg_cmd)
        cleanup_templates("mydb")

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

        def fake_pg_cmd(
            cmd: list[str], *, sudo_user: str = "postgres"
        ) -> tuple[int, str, str]:
            captured_cmds.append(cmd)
            return (0, "", "")

        monkeypatch.setattr("fraisier.dbops.templates._pg_cmd", fake_pg_cmd)
        cleanup_templates("mydb")

        # Find the -c argument (the SQL string)
        sql_cmd = captured_cmds[0]
        c_index = sql_cmd.index("-c")
        sql_string = sql_cmd[c_index + 1]
        # SQL string must NOT contain the literal db name
        assert "mydb" not in sql_string

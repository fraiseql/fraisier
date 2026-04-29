"""Tests for fraisier.dbops.operations module."""

from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.operations import (
    _pg_cmd,
    check_db_exists,
    create_db,
    drop_db,
    run_psql,
    run_sql,
    terminate_backends,
)

_TEST_URL = "postgresql://postgres:pass@localhost:5432/mydb"


class TestPgCmd:
    """Test the low-level _pg_cmd helper."""

    def test_pg_cmd_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
            code, stdout, stderr = _pg_cmd(
                ["psql", "-c", "SELECT 1"], connection_url=_TEST_URL
            )

        assert code == 0
        assert stdout == "ok\n"
        assert stderr == ""
        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "psql",
            "-h",
            "localhost",
            "-p",
            "5432",
            "-U",
            "postgres",
            "-d",
            "mydb",
            "-c",
            "SELECT 1",
        ]
        assert "sudo" not in cmd

    def test_pg_cmd_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=2, stdout="", stderr="fatal error"
            )
            code, _stdout, stderr = _pg_cmd(
                ["dropdb", "nope"], connection_url=_TEST_URL
            )

        assert code == 2
        assert stderr == "fatal error"

    def test_pg_cmd_requires_connection_url(self):
        """connection_url is a required keyword argument."""
        with pytest.raises(TypeError):
            _pg_cmd(["psql", "-c", "SELECT 1"])  # ty: ignore[missing-argument]

    def test_pg_cmd_passes_password_via_env(self):
        """Password from URL is forwarded to subprocess via PGPASSWORD."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(
                ["psql", "-d", "mydb", "-c", "SELECT 1"],
                connection_url=_TEST_URL,
            )

        env = mock_run.call_args.kwargs["env"]
        assert env is not None
        assert env["PGPASSWORD"] == "pass"


class TestPgCmdDatabaseInjection:
    """Test that _pg_cmd injects the database from the URL (#185)."""

    def test_injects_db_from_url_when_no_d_flag(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(
                ["psql", "-c", "SELECT 1"],
                connection_url="postgresql://user@localhost/postgres",
            )
        cmd = mock_run.call_args[0][0]
        assert "-d" in cmd
        assert cmd[cmd.index("-d") + 1] == "postgres"

    def test_does_not_inject_db_when_d_already_present(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(
                ["psql", "-d", "mydb", "-c", "SELECT 1"],
                connection_url="postgresql://user@localhost/postgres",
            )
        cmd = mock_run.call_args[0][0]
        assert cmd.count("-d") == 1
        assert cmd[cmd.index("-d") + 1] == "mydb"

    def test_no_db_injection_when_url_path_empty(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(
                ["psql", "-c", "SELECT 1"],
                connection_url="postgresql://user@localhost/",
            )
        cmd = mock_run.call_args[0][0]
        assert "-d" not in cmd

    def test_injects_db_from_socket_url(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(
                ["psql", "-c", "SELECT 1"],
                connection_url="postgresql:///postgres?host=/var/run/postgresql",
            )
        cmd = mock_run.call_args[0][0]
        assert "-d" in cmd
        assert cmd[cmd.index("-d") + 1] == "postgres"

    def test_injects_maintenance_db_for_createdb(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(
                ["createdb", "newdb"],
                connection_url="postgresql://user@localhost/postgres",
            )
        cmd = mock_run.call_args[0][0]
        assert "--maintenance-db" in cmd
        assert cmd[cmd.index("--maintenance-db") + 1] == "postgres"

    def test_injects_maintenance_db_for_dropdb(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(
                ["dropdb", "--if-exists", "olddb"],
                connection_url="postgresql://user@localhost/postgres",
            )
        cmd = mock_run.call_args[0][0]
        assert "--maintenance-db" in cmd
        assert cmd[cmd.index("--maintenance-db") + 1] == "postgres"

    def test_does_not_inject_maintenance_db_when_already_present(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(
                ["createdb", "--maintenance-db", "template1", "newdb"],
                connection_url="postgresql://user@localhost/postgres",
            )
        cmd = mock_run.call_args[0][0]
        assert cmd.count("--maintenance-db") == 1
        assert cmd[cmd.index("--maintenance-db") + 1] == "template1"

    def test_no_injection_for_pg_dump(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(
                ["pg_dump", "-Fc", "mydb"],
                connection_url="postgresql://user@localhost/postgres",
            )
        cmd = mock_run.call_args[0][0]
        assert "-d" not in cmd
        assert "--maintenance-db" not in cmd


class TestRunPsql:
    """Test run_psql wrapper."""

    def test_run_psql(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="result\n", stderr=""
            )
            code, stdout, _ = run_psql(
                "SELECT 1", db_name="mydb", connection_url=_TEST_URL
            )

        assert code == 0
        assert stdout == "result\n"
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "psql"
        assert "sudo" not in cmd
        assert "-d" in cmd
        assert "mydb" in cmd
        assert "SELECT 1" in cmd

    def test_run_psql_requires_connection_url(self):
        with pytest.raises(TypeError):
            run_psql("SELECT 1", db_name="mydb")  # ty: ignore[missing-argument]


class TestRunSql:
    """Test run_sql wrapper with tuples-only output."""

    def test_run_sql(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="42\n", stderr="")
            code, stdout, _ = run_sql(
                "SELECT count(*) FROM pg_tables",
                db_name="mydb",
                connection_url=_TEST_URL,
            )

        assert code == 0
        assert stdout == "42\n"
        cmd = mock_run.call_args[0][0]
        assert "-t" in cmd
        assert "-A" in cmd
        assert cmd[0] == "psql"
        assert "sudo" not in cmd


class TestCheckDbExists:
    """Test check_db_exists."""

    def test_check_db_exists_true(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="1\n", stderr="")
            assert check_db_exists("mydb", connection_url=_TEST_URL) is True

    def test_check_db_exists_false(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="0\n", stderr="")
            assert check_db_exists("mydb", connection_url=_TEST_URL) is False

    def test_check_db_exists_error(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="connection refused"
            )
            assert check_db_exists("mydb", connection_url=_TEST_URL) is False

    def test_check_db_exists_uses_url_database(self):
        url = "postgresql://user@localhost/postgres"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="1\n", stderr="")
            check_db_exists("mydb", connection_url=url)
        cmd = mock_run.call_args[0][0]
        assert "-d" in cmd
        assert cmd[cmd.index("-d") + 1] == "postgres"

    def test_check_db_exists_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            check_db_exists("mydb; DROP TABLE users", connection_url=_TEST_URL)


class TestTerminateBackends:
    """Test terminate_backends."""

    def test_terminate_backends(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="t\n", stderr="")
            code, _stdout, _ = terminate_backends("mydb", connection_url=_TEST_URL)

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert "psql" in cmd
        assert any("pg_terminate_backend" in arg for arg in cmd)

    def test_terminate_backends_uses_url_database(self):
        url = "postgresql://user@localhost/postgres"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="t\n", stderr="")
            terminate_backends("mydb", connection_url=url)
        cmd = mock_run.call_args[0][0]
        assert "-d" in cmd
        assert cmd[cmd.index("-d") + 1] == "postgres"

    def test_terminate_backends_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            terminate_backends("db'; DROP TABLE x;--", connection_url=_TEST_URL)


class TestDropDb:
    """Test drop_db."""

    def test_drop_db_simple(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = drop_db("testdb", connection_url=_TEST_URL)

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "dropdb"
        assert "testdb" in cmd
        assert "sudo" not in cmd

    def test_drop_db_force_disconnect(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = drop_db(
                "testdb", force_disconnect=True, connection_url=_TEST_URL
            )

        assert code == 0
        # Two calls: terminate_backends then dropdb
        assert mock_run.call_count == 2
        terminate_cmd = mock_run.call_args_list[0][0][0]
        assert any("pg_terminate_backend" in arg for arg in terminate_cmd)
        drop_cmd = mock_run.call_args_list[1][0][0]
        assert "dropdb" in drop_cmd

    def test_drop_db_force(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = drop_db("testdb", force=True, connection_url=_TEST_URL)

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "psql"
        assert "-c" in cmd
        sql_index = cmd.index("-c") + 1
        sql = cmd[sql_index]
        assert "DROP DATABASE IF EXISTS testdb WITH (FORCE)" in sql

    def test_drop_db_force_uses_url_database(self):
        url = "postgresql://user@localhost/postgres"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            drop_db("testdb", force=True, connection_url=url)
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "psql"
        assert "-d" in cmd
        assert cmd[cmd.index("-d") + 1] == "postgres"

    def test_drop_db_simple_uses_maintenance_db(self):
        url = "postgresql://user@localhost/postgres"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            drop_db("testdb", connection_url=url)
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "dropdb"
        assert "--maintenance-db" in cmd
        assert cmd[cmd.index("--maintenance-db") + 1] == "postgres"

    def test_drop_db_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            drop_db("test; rm -rf /", connection_url=_TEST_URL)


class TestCreateDb:
    """Test create_db."""

    def test_create_db_simple(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = create_db("newdb", connection_url=_TEST_URL)

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "createdb"
        assert "newdb" in cmd
        assert "sudo" not in cmd

    def test_create_db_with_template_and_owner(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = create_db(
                "newdb",
                template="tmpl",
                owner="appuser",
                connection_url=_TEST_URL,
            )

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "createdb"
        assert "-T" in cmd
        assert "tmpl" in cmd
        assert "-O" in cmd
        assert "appuser" in cmd
        assert "newdb" in cmd
        assert "sudo" not in cmd

    def test_create_db_uses_maintenance_db(self):
        url = "postgresql://user@localhost/postgres"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            create_db("newdb", connection_url=url)
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "createdb"
        assert "--maintenance-db" in cmd
        assert cmd[cmd.index("--maintenance-db") + 1] == "postgres"

    def test_create_db_rejects_bad_template(self):
        with pytest.raises(ValueError, match="Invalid template name"):
            create_db("newdb", template="bad template!", connection_url=_TEST_URL)

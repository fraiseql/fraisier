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


class TestPgCmd:
    """Test the low-level _pg_cmd helper."""

    def test_pg_cmd_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok\n", stderr="")
            code, stdout, stderr = _pg_cmd(["psql", "-c", "SELECT 1"])

        assert code == 0
        assert stdout == "ok\n"
        assert stderr == ""
        cmd = mock_run.call_args[0][0]
        assert cmd == ["sudo", "-u", "postgres", "psql", "-c", "SELECT 1"]

    def test_pg_cmd_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=2, stdout="", stderr="fatal error"
            )
            code, _stdout, stderr = _pg_cmd(["dropdb", "nope"])

        assert code == 2
        assert stderr == "fatal error"


class TestRunPsql:
    """Test run_psql wrapper."""

    def test_run_psql(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="result\n", stderr=""
            )
            code, stdout, _ = run_psql("SELECT 1", db_name="mydb")

        assert code == 0
        assert stdout == "result\n"
        cmd = mock_run.call_args[0][0]
        assert cmd == ["sudo", "-u", "postgres", "psql", "-d", "mydb", "-c", "SELECT 1"]


class TestRunSql:
    """Test run_sql wrapper with tuples-only output."""

    def test_run_sql(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="42\n", stderr="")
            code, stdout, _ = run_sql("SELECT count(*) FROM pg_tables", db_name="mydb")

        assert code == 0
        assert stdout == "42\n"
        cmd = mock_run.call_args[0][0]
        assert "-t" in cmd
        assert "-A" in cmd
        assert cmd == [
            "sudo",
            "-u",
            "postgres",
            "psql",
            "-d",
            "mydb",
            "-t",
            "-A",
            "-c",
            "SELECT count(*) FROM pg_tables",
        ]


class TestCheckDbExists:
    """Test check_db_exists."""

    def test_check_db_exists_true(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="1\n", stderr="")
            assert check_db_exists("mydb") is True

    def test_check_db_exists_false(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="0\n", stderr="")
            assert check_db_exists("mydb") is False

    def test_check_db_exists_error(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="connection refused"
            )
            assert check_db_exists("mydb") is False

    def test_check_db_exists_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            check_db_exists("mydb; DROP TABLE users")


class TestTerminateBackends:
    """Test terminate_backends."""

    def test_terminate_backends(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="t\n", stderr="")
            code, _stdout, _ = terminate_backends("mydb")

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert "psql" in cmd
        assert any("pg_terminate_backend" in arg for arg in cmd)

    def test_terminate_backends_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            terminate_backends("db'; DROP TABLE x;--")


class TestDropDb:
    """Test drop_db."""

    def test_drop_db_simple(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = drop_db("testdb")

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert cmd == ["sudo", "-u", "postgres", "dropdb", "testdb"]

    def test_drop_db_force_disconnect(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = drop_db("testdb", force_disconnect=True)

        assert code == 0
        # Two calls: terminate_backends then dropdb
        assert mock_run.call_count == 2
        terminate_cmd = mock_run.call_args_list[0][0][0]
        assert any("pg_terminate_backend" in arg for arg in terminate_cmd)
        drop_cmd = mock_run.call_args_list[1][0][0]
        assert "dropdb" in drop_cmd

    def test_drop_db_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            drop_db("test; rm -rf /")


class TestCreateDb:
    """Test create_db."""

    def test_create_db_simple(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = create_db("newdb")

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert cmd == ["sudo", "-u", "postgres", "createdb", "newdb"]

    def test_create_db_with_template_and_owner(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = create_db("newdb", template="tmpl", owner="appuser")

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "sudo",
            "-u",
            "postgres",
            "createdb",
            "-T",
            "tmpl",
            "-O",
            "appuser",
            "newdb",
        ]

    def test_create_db_rejects_bad_template(self):
        with pytest.raises(ValueError, match="Invalid template name"):
            create_db("newdb", template="bad template!")

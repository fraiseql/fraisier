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
    unset_template_flag,
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
        assert cmd == ["psql", "-d", "mydb", "-c", "SELECT 1"]

    def test_pg_cmd_carries_the_connection_in_env(self):
        """Only the database name is on argv (#493); libpq reads the rest."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(["psql", "-c", "SELECT 1"], connection_url=_TEST_URL)

        env = mock_run.call_args.kwargs["env"]
        assert {k: env[k] for k in env if k.startswith("PG")} == {
            "PGUSER": "postgres",
            "PGPASSWORD": "pass",
            "PGHOST": "localhost",
            "PGPORT": "5432",
            "PGDATABASE": "mydb",
        }

    @pytest.mark.parametrize(
        ("url", "password"),
        [
            ("postgresql://u:p%40ss@h/db", "p@ss"),
            ("postgresql://u@h/db?password=pw", "pw"),
            ("postgresql://u@h/db?password=a+b", "a+b"),
        ],
    )
    def test_pg_cmd_password_is_decoded_as_libpq_decodes_it(self, url, password):
        """#493's acceptance cases."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(["psql", "-c", "SELECT 1"], connection_url=url)

        assert mock_run.call_args.kwargs["env"]["PGPASSWORD"] == password
        assert not [a for a in mock_run.call_args[0][0] if password in a]

    def test_pg_cmd_db_flag_follows_the_query_dbname(self):
        """libpq's ``?dbname=`` beats the path; the argv flag must agree."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(
                ["dropdb", "x"], connection_url="postgresql://h/postgres?dbname=admin"
            )

        assert mock_run.call_args[0][0] == ["dropdb", "--maintenance-db", "admin", "x"]

    def test_pg_cmd_drops_ambient_pg_variables(self, monkeypatch):
        for name in ("PGUSER", "PGPASSWORD", "PGOPTIONS"):
            monkeypatch.setenv(name, "ambient")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _pg_cmd(["psql", "-c", "SELECT 1"], connection_url="postgresql:///db")

        env = mock_run.call_args.kwargs["env"]
        assert [k for k in env if env[k] == "ambient"] == []

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

    def test_a_url_naming_no_database_is_refused(self):
        """libpq would default the database to the user name (refused)."""
        with patch("subprocess.run") as mock_run, pytest.raises(ValueError):
            _pg_cmd(
                ["psql", "-c", "SELECT 1"],
                connection_url="postgresql://user@localhost/",
            )
        mock_run.assert_not_called()

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

    def test_drop_db_clear_template_flag_runs_update_first(self):
        """clear_template_flag=True clears datistemplate before dropping (#200)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = drop_db(
                "mytpl", clear_template_flag=True, connection_url=_TEST_URL
            )

        assert code == 0
        assert mock_run.call_count == 2
        update_cmd = mock_run.call_args_list[0][0][0]
        assert update_cmd[0] == "psql"
        sql = update_cmd[update_cmd.index("-c") + 1]
        assert sql == (
            "UPDATE pg_database SET datistemplate=false WHERE datname='mytpl'"
        )
        drop_cmd = mock_run.call_args_list[1][0][0]
        assert drop_cmd[0] == "dropdb"
        assert "mytpl" in drop_cmd

    def test_drop_db_clear_template_flag_with_force(self):
        """clear_template_flag composes with force (UPDATE then DROP ... WITH FORCE)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            drop_db(
                "mytpl",
                clear_template_flag=True,
                force=True,
                connection_url=_TEST_URL,
            )

        assert mock_run.call_count == 2
        update_cmd = mock_run.call_args_list[0][0][0]
        assert "datistemplate=false" in update_cmd[update_cmd.index("-c") + 1]
        drop_cmd = mock_run.call_args_list[1][0][0]
        assert drop_cmd[0] == "psql"
        assert (
            "DROP DATABASE IF EXISTS mytpl WITH (FORCE)"
            in (drop_cmd[drop_cmd.index("-c") + 1])
        )

    def test_drop_db_default_does_not_unset_template(self):
        """Without clear_template_flag, only dropdb runs (no extra UPDATE)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            drop_db("testdb", connection_url=_TEST_URL)

        assert mock_run.call_count == 1
        assert mock_run.call_args_list[0][0][0][0] == "dropdb"


class TestUnsetTemplateFlag:
    """Test unset_template_flag (#200)."""

    def test_unset_template_flag_runs_expected_sql(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            code, _, _ = unset_template_flag("mytpl", connection_url=_TEST_URL)

        assert code == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "psql"
        assert "-c" in cmd
        sql = cmd[cmd.index("-c") + 1]
        assert sql == "UPDATE pg_database SET datistemplate=false WHERE datname='mytpl'"

    def test_unset_template_flag_rejects_injection(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            unset_template_flag("tpl'; DROP TABLE x;--", connection_url=_TEST_URL)


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


def _run_create_db(**kwargs):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        create_db("newdb", connection_url=_TEST_URL, **kwargs)
    return mock_run.call_args


class TestCreateDbStrategy:
    """#479: a template copy can ask for ``STRATEGY = FILE_COPY``."""

    def test_file_copy_runs_a_psql_script_on_stdin(self):
        call = _run_create_db(template="tmpl", strategy="file_copy")
        cmd = call[0][0]
        assert cmd[0] == "psql"
        assert cmd[-2:] == ["-f", "-"]
        assert "STRATEGY = FILE_COPY" in call.kwargs["input"]

    def test_file_copy_binds_the_names_instead_of_quoting_them(self):
        call = _run_create_db(template="tmpl", owner="appuser", strategy="file_copy")
        cmd = call[0][0]
        assert "db_name=newdb" in cmd
        assert "template=tmpl" in cmd
        assert "owner=appuser" in cmd
        script = call.kwargs["input"]
        assert 'CREATE DATABASE :"db_name" TEMPLATE :"template" OWNER :"owner"' in (
            script
        )
        assert "newdb" not in script
        assert "tmpl" not in script

    def test_file_copy_stops_on_the_first_error(self):
        # `psql -f` exits 0 after a failed statement without it.
        cmd = _run_create_db(template="tmpl", strategy="file_copy")[0][0]
        assert cmd[cmd.index("ON_ERROR_STOP=1") - 1] == "-v"

    def test_file_copy_omits_the_strategy_below_postgres_15(self):
        # PG 14 answers `STRATEGY` with: ERROR:  option "strategy" not recognized
        script = _run_create_db(template="tmpl", strategy="file_copy").kwargs["input"]
        assert "current_setting('server_version_num')::int >= 150000" in script
        with_strategy, without = script.split("\\else")
        assert "STRATEGY = FILE_COPY" in with_strategy
        assert "STRATEGY" not in without
        assert 'CREATE DATABASE :"db_name" TEMPLATE :"template";' in without

    def test_file_copy_without_owner_binds_no_owner(self):
        call = _run_create_db(template="tmpl", strategy="file_copy")
        assert not any(arg.startswith("owner=") for arg in call[0][0])
        assert "OWNER" not in call.kwargs["input"]

    @pytest.mark.parametrize("strategy", [None, "wal_log"])
    def test_wal_log_and_none_keep_the_createdb_command(self, strategy):
        call = _run_create_db(template="tmpl", strategy=strategy)
        cmd = call[0][0]
        assert cmd[0] == "createdb"
        assert "-S" not in cmd
        assert "--strategy" not in cmd
        assert call.kwargs.get("input") is None

    def test_unknown_strategy_is_refused(self):
        with pytest.raises(ValueError, match="template strategy"):
            create_db(
                "newdb", template="tmpl", strategy="bogus", connection_url=_TEST_URL
            )

    def test_file_copy_still_validates_identifiers(self):
        with pytest.raises(ValueError, match="Invalid template name"):
            create_db(
                "newdb",
                template='x"; DROP DATABASE y; --',
                strategy="file_copy",
                connection_url=_TEST_URL,
            )

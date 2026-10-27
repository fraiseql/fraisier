"""Tests for `fraisier db exec` command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main

_DB_NAME = "acme_db_production"
_ADMIN_URL = "postgresql://postgres@localhost/postgres"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def local_config():
    """Config with no SSH block (local environment)."""
    cfg = MagicMock()
    cfg.get_fraise.return_value = {"type": "api"}
    cfg.get_fraise_environment.return_value = {
        "type": "api",
        "database": {
            "name": _DB_NAME,
            "admin_url": _ADMIN_URL,
        },
    }
    with patch("fraisier.cli.main.get_config", return_value=cfg):
        yield cfg


class TestDbExecLocal:
    def test_select_succeeds(self, runner, local_config):
        mock_result = MagicMock(stdout="1\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = runner.invoke(
                main, ["db", "exec", "api", "-e", "staging", "SELECT 1"]
            )
        assert result.exit_code == 0
        mock_run.assert_called_once()
        argv = mock_run.call_args[0][0]
        assert argv[0] == "psql"
        assert _DB_NAME in argv or _ADMIN_URL in argv

    def test_explain_analyze_succeeds(self, runner, local_config):
        sql = "EXPLAIN ANALYZE SELECT * FROM public.tb_user"
        mock_result = MagicMock(stdout="Seq Scan...\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = runner.invoke(main, ["db", "exec", "api", "-e", "staging", sql])
        assert result.exit_code == 0

    def test_insert_rejected_without_write(self, runner, local_config):
        result = runner.invoke(
            main, ["db", "exec", "api", "-e", "staging", "INSERT INTO foo VALUES (1)"]
        )
        assert result.exit_code != 0
        assert "read-only" in result.output.lower() or "write" in result.output.lower()

    def test_missing_fraise_exits_1(self, runner, local_config):
        local_config.get_fraise.return_value = None
        result = runner.invoke(
            main, ["db", "exec", "nonexistent", "-e", "staging", "SELECT 1"]
        )
        assert result.exit_code == 1

    def test_missing_environment_exits_1(self, runner, local_config):
        local_config.get_fraise_environment.return_value = None
        result = runner.invoke(
            main, ["db", "exec", "api", "-e", "nonexistent", "SELECT 1"]
        )
        assert result.exit_code == 1

    def test_missing_database_config_exits_1(self, runner, local_config):
        local_config.get_fraise_environment.return_value = {
            "type": "api",
        }
        result = runner.invoke(main, ["db", "exec", "api", "-e", "staging", "SELECT 1"])
        assert result.exit_code == 1

    def test_csv_format_passes_csv_flag(self, runner, local_config):
        mock_result = MagicMock(stdout="id,name\n1,foo\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = runner.invoke(
                main, ["db", "exec", "api", "-e", "staging", "--csv", "SELECT 1"]
            )
        assert result.exit_code == 0
        argv = mock_run.call_args[0][0]
        assert "--csv" in argv

    def test_file_flag_reads_sql_from_file(self, runner, local_config, tmp_path):
        sql_file = tmp_path / "query.sql"
        sql_file.write_text("SELECT count(*) FROM pg_tables")
        mock_result = MagicMock(stdout="100\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = runner.invoke(
                main,
                ["db", "exec", "api", "-e", "staging", "--file", str(sql_file)],
            )
        assert result.exit_code == 0
        argv = mock_run.call_args[0][0]
        assert "SELECT count(*) FROM pg_tables" in " ".join(argv)

    def test_sql_arg_and_file_mutually_exclusive(self, runner, local_config, tmp_path):
        sql_file = tmp_path / "query.sql"
        sql_file.write_text("SELECT 1")
        result = runner.invoke(
            main,
            ["db", "exec", "api", "-e", "staging", "--file", str(sql_file), "SELECT 2"],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    def test_psql_nonzero_exit_propagates(self, runner, local_config):
        mock_result = MagicMock(
            stdout="", stderr="ERROR: relation not found", returncode=1
        )
        with patch("subprocess.run", return_value=mock_result):
            result = runner.invoke(
                main, ["db", "exec", "api", "-e", "staging", "SELECT 1"]
            )
        assert result.exit_code != 0

    def test_timeout_default_30s(self, runner, local_config):
        mock_result = MagicMock(stdout="", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner.invoke(main, ["db", "exec", "api", "-e", "staging", "SELECT 1"])
        argv = mock_run.call_args[0][0]
        assert "30000" in " ".join(argv)

    def test_custom_timeout(self, runner, local_config):
        mock_result = MagicMock(stdout="", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = runner.invoke(
                main,
                ["db", "exec", "api", "-e", "staging", "--timeout", "60", "SELECT 1"],
            )
        assert result.exit_code == 0
        argv = mock_run.call_args[0][0]
        assert "60000" in " ".join(argv)


@pytest.fixture
def remote_config():
    """Config with SSH block (remote environment)."""
    cfg = MagicMock()
    cfg.get_fraise.return_value = {"type": "api"}
    cfg.get_fraise_environment.return_value = {
        "type": "api",
        "database": {
            "name": _DB_NAME,
            "admin_url": _ADMIN_URL,
        },
        "ssh": {
            "host": "prod.example.com",
            "user": "deploy",
        },
    }
    with patch("fraisier.cli.main.get_config", return_value=cfg):
        yield cfg


class TestDbExecRemote:
    def test_select_routes_through_ssh(self, runner, remote_config):
        """Through an SSHRunner, the one SSH channel that carries an env off
        argv (#492): ``ssh.short_cmd`` put the admin URL on the remote
        command line."""
        mock_proc = MagicMock(stdout="1\n", returncode=0)
        with patch(
            "fraisier.runners.SSHRunner.run", autospec=True, return_value=mock_proc
        ) as run:
            result = runner.invoke(
                main, ["db", "exec", "api", "-e", "staging", "SELECT 1"]
            )
        assert result.exit_code == 0
        run.assert_called_once()
        ssh_runner, argv = run.call_args.args
        assert ssh_runner.host == "prod.example.com"
        assert ssh_runner.user == "deploy"
        assert argv[0] == "psql"

    def test_no_local_psql_for_remote(self, runner, remote_config):
        mock_proc = MagicMock(stdout="1\n", returncode=0)
        with (
            patch("fraisier.runners.SSHRunner.run", return_value=mock_proc),
            patch("subprocess.run") as mock_local,
        ):
            runner.invoke(main, ["db", "exec", "api", "-e", "staging", "SELECT 1"])
        mock_local.assert_not_called()

    def test_ssh_error_exits_nonzero(self, runner, remote_config):
        import subprocess

        with patch(
            "fraisier.runners.SSHRunner.run",
            side_effect=subprocess.CalledProcessError(
                1, "psql", stderr="connection refused"
            ),
        ):
            result = runner.invoke(
                main, ["db", "exec", "api", "-e", "staging", "SELECT 1"]
            )
        assert result.exit_code != 0
        assert "Error" in result.output


_SECRET_URL = "postgresql://postgres:s3cr3t@localhost/postgres"


class TestDbExecConnection:
    """psql reads the admin URL through ``PgConn`` (#493): the password off
    argv on both ends (#491), no ambient ``PG*`` but the operator defaults."""

    @pytest.fixture
    def secret_url(self, local_config):
        local_config.get_fraise_environment.return_value = {
            "type": "api",
            "database": {"name": _DB_NAME, "admin_url": _SECRET_URL},
        }
        return local_config

    def test_local_password_is_off_argv(self, runner, secret_url, monkeypatch):
        monkeypatch.setenv("PGSERVICE", "acme")
        mock_result = MagicMock(stdout="1\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = runner.invoke(
                main, ["db", "exec", "api", "-e", "staging", "SELECT 1"]
            )

        assert result.exit_code == 0, result.output
        argv, env = mock_run.call_args.args[0], mock_run.call_args.kwargs["env"]
        assert not any("s3cr3t" in arg for arg in argv)
        assert env["PGPASSWORD"] == "s3cr3t"
        assert "PGSERVICE" not in env

    def test_remote_password_is_off_both_argvs(self, runner, remote_config):
        remote_config.get_fraise_environment.return_value["database"]["admin_url"] = (
            _SECRET_URL
        )
        done = MagicMock(stdout="1\n", returncode=0)
        with (
            patch("fraisier.runners.SSHRunner.run", return_value=done) as run,
            patch("fraisier.ssh.short_cmd") as short_cmd,
        ):
            result = runner.invoke(
                main, ["db", "exec", "api", "-e", "staging", "SELECT 1"]
            )

        assert result.exit_code == 0, result.output
        short_cmd.assert_not_called()
        argv, kwargs = run.call_args.args[0], run.call_args.kwargs
        assert not any("s3cr3t" in arg for arg in argv)
        assert kwargs["env"]["PGPASSWORD"] == "s3cr3t"
        assert kwargs["scrub_pg"] is True

    def test_a_refused_url_runs_nothing(self, runner, secret_url):
        secret_url.get_fraise_environment.return_value["database"]["admin_url"] = (
            "postgresql://postgres@localhost/postgres?service=acme"
        )
        with patch("subprocess.run") as mock_run:
            result = runner.invoke(
                main, ["db", "exec", "api", "-e", "staging", "SELECT 1"]
            )

        assert result.exit_code == 1
        assert "service" in result.output
        mock_run.assert_not_called()


class TestDbExecProductionGate:
    def test_production_prompts_for_confirmation(self, runner, local_config):
        """Production gate: user is prompted and must confirm."""
        local_config.get_fraise_environment.return_value = {
            "type": "api",
            "database": {"name": _DB_NAME, "admin_url": _ADMIN_URL},
        }
        mock_result = MagicMock(stdout="1\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = runner.invoke(
                main,
                ["db", "exec", "api", "-e", "production", "SELECT 1"],
                input="y\n",
            )
        assert result.exit_code == 0
        assert "production" in result.output.lower()

    def test_production_aborts_on_no(self, runner, local_config):
        """Production gate: 'n' aborts without executing."""
        mock_result = MagicMock(stdout="1\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = runner.invoke(
                main,
                ["db", "exec", "api", "-e", "production", "SELECT 1"],
                input="n\n",
            )
        assert result.exit_code != 0
        mock_run.assert_not_called()

    def test_staging_does_not_prompt(self, runner, local_config):
        """Non-production environments skip the confirmation prompt."""
        mock_result = MagicMock(stdout="1\n", returncode=0)
        with patch("subprocess.run", return_value=mock_result):
            result = runner.invoke(
                main, ["db", "exec", "api", "-e", "staging", "SELECT 1"]
            )
        assert result.exit_code == 0
        assert "Continue?" not in result.output


class TestDbExecEdgeCases:
    def test_file_not_found_exits_gracefully(self, runner, local_config):
        result = runner.invoke(
            main,
            ["db", "exec", "api", "-e", "staging", "--file", "/nonexistent/query.sql"],
        )
        assert result.exit_code != 0

    def test_empty_sql_rejected(self, runner, local_config):
        result = runner.invoke(main, ["db", "exec", "api", "-e", "staging", "   "])
        assert result.exit_code != 0
        assert "read-only" in result.output.lower() or "empty" in result.output.lower()

    def test_comment_only_sql_rejected(self, runner, local_config):
        result = runner.invoke(
            main, ["db", "exec", "api", "-e", "staging", "-- just a comment"]
        )
        assert result.exit_code != 0

    def test_timeout_zero_rejected(self, runner, local_config):
        result = runner.invoke(
            main, ["db", "exec", "api", "-e", "staging", "--timeout", "0", "SELECT 1"]
        )
        assert result.exit_code != 0

    def test_timeout_negative_rejected(self, runner, local_config):
        result = runner.invoke(
            main, ["db", "exec", "api", "-e", "staging", "--timeout", "-5", "SELECT 1"]
        )
        assert result.exit_code != 0

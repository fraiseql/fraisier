"""Tests for the ``query_database_size_mb`` helper (#201 follow-up).

Samples ``pg_database_size(current_database())`` against the app DB and
converts bytes → MB. Best-effort: any psql failure returns ``None`` so
the recorder still writes the row.
"""

from __future__ import annotations

import logging
import subprocess
from unittest.mock import MagicMock

from fraisier.dbops.sizing import query_database_size_mb


def _runner_returning(stdout: str) -> MagicMock:
    runner = MagicMock()
    runner.run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=""
    )
    return runner


class TestQueryDatabaseSizeMb:
    def test_returns_size_in_mb_from_psql_stdout(self):
        # 5 MiB exactly (5 * 1024 * 1024).
        runner = _runner_returning("5242880\n")
        assert (
            query_database_size_mb("postgresql://u@h/db", runner=runner) == 5
        )

    def test_returns_none_when_psql_exits_nonzero(self):
        runner = MagicMock()
        runner.run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["psql"], stderr="FATAL: db unreachable"
        )
        assert (
            query_database_size_mb("postgresql://u@h/db", runner=runner) is None
        )

    def test_returns_none_on_unparseable_output(self):
        runner = _runner_returning("not a number")
        assert (
            query_database_size_mb("postgresql://u@h/db", runner=runner) is None
        )

    def test_invokes_psql_with_database_url_and_pg_database_size_query(self):
        runner = _runner_returning("1048576\n")
        query_database_size_mb("postgresql://u@h/db", runner=runner)
        runner.run.assert_called_once()
        # First positional arg is the command list.
        assert runner.run.call_args.args[0] == [
            "psql",
            "postgresql://u@h/db",
            "-tAc",
            "SELECT pg_database_size(current_database())",
        ]

    def test_returns_zero_when_size_is_under_one_mb(self):
        # ~488 KiB → integer divide → 0 MB; estimator floor will dominate.
        runner = _runner_returning("500000")
        assert (
            query_database_size_mb("postgresql://u@h/db", runner=runner) == 0
        )

    def test_returns_none_on_oserror(self):
        runner = MagicMock()
        runner.run.side_effect = OSError("psql binary not found")
        assert (
            query_database_size_mb("postgresql://u@h/db", runner=runner) is None
        )

    def test_log_message_redacts_password(self, caplog):
        runner = MagicMock()
        runner.run.side_effect = subprocess.CalledProcessError(
            returncode=2, cmd=["psql"], stderr="auth failed"
        )
        with caplog.at_level(logging.DEBUG, logger="fraisier.dbops.sizing"):
            result = query_database_size_mb(
                "postgresql://user:supersecret@db.example/prod_db",
                runner=runner,
            )
        assert result is None
        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "supersecret" not in joined
        # The redacted shape should still surface host + dbname so the
        # misconfig is diagnosable.
        assert "db.example" in joined
        assert "prod_db" in joined

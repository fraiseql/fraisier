"""Tests for post-migrate SQL hook (#204 PR A).

The post-migrate hook runs a configurable list of SQL files (typically
``db/7_grant/*.sql``) between ``confiture migrate up`` and the service
restart so cross-script reconciliation (idempotent grant sweeps,
post-migration fixups) lives outside confiture's per-migration scope.

Two on_error modes:
- ``halt`` (default): psql nonzero exit raises ``DeploymentError``
  immediately; no further entries run; the deploy aborts before the
  service is restarted.
- ``warn``: psql nonzero exit is logged at WARNING and iteration
  continues to the next entry. The deploy still ends in ``success``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fraisier.errors import DeploymentError
from fraisier.post_migrate import (
    PostMigrateStep,
    load_post_migrate_steps,
    run_configured_post_migrate,
    run_post_migrate_steps,
)

# ---------------------------------------------------------------------------
# Loader / path resolution
# ---------------------------------------------------------------------------


class TestLoadPostMigrateSteps:
    def test_missing_post_migrate_key_is_noop(self):
        # The `database:` block has no `post_migrate:` entry — no steps.
        steps = load_post_migrate_steps(
            {"strategy": "rebuild"}, app_path=Path("/srv/api")
        )
        assert steps == []

    def test_empty_post_migrate_section_is_noop(self):
        steps = load_post_migrate_steps(
            {"strategy": "rebuild", "post_migrate": []},
            app_path=Path("/srv/api"),
        )
        assert steps == []

    def test_path_resolution_is_relative_to_app_path(self):
        steps = load_post_migrate_steps(
            {
                "post_migrate": [
                    {"sql_dir": "db/7_grant/"},
                    {"sql_file": "db/post_migrate.sql"},
                ]
            },
            app_path=Path("/srv/api"),
        )
        assert len(steps) == 2
        assert steps[0].sql_dir == Path("/srv/api/db/7_grant/")
        assert steps[0].sql_file is None
        assert steps[1].sql_file == Path("/srv/api/db/post_migrate.sql")
        assert steps[1].sql_dir is None

    def test_on_error_defaults_to_halt(self):
        steps = load_post_migrate_steps(
            {"post_migrate": [{"sql_file": "db/x.sql"}]},
            app_path=Path("/srv/api"),
        )
        assert steps[0].on_error == "halt"

    def test_absolute_paths_are_preserved(self):
        steps = load_post_migrate_steps(
            {"post_migrate": [{"sql_file": "/etc/extra/grant.sql"}]},
            app_path=Path("/srv/api"),
        )
        assert steps[0].sql_file == Path("/etc/extra/grant.sql")


# ---------------------------------------------------------------------------
# Runner — single-file
# ---------------------------------------------------------------------------


def _runner_ok() -> MagicMock:
    runner = MagicMock()
    runner.run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    return runner


class TestRunPostMigrateStepsSingleFile:
    def test_runs_single_sql_file_via_psql(self, tmp_path):
        sql = tmp_path / "grant.sql"
        sql.write_text("GRANT SELECT ON tb_foo TO app;")
        steps = [PostMigrateStep(sql_dir=None, sql_file=sql, on_error="halt")]
        runner = _runner_ok()

        run_post_migrate_steps(
            steps,
            database_url="postgresql://u@h/db",
            runner=runner,
        )

        runner.run.assert_called_once()
        cmd = runner.run.call_args.args[0]
        assert cmd == [
            "psql",
            "postgresql://u@h/db",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            str(sql),
        ]


class TestRunPostMigrateStepsDirectory:
    def test_runs_all_files_in_dir_lexicographically(self, tmp_path):
        sql_dir = tmp_path / "grants"
        sql_dir.mkdir()
        (sql_dir / "20_grants.sql").write_text("-- second")
        (sql_dir / "10_revoke.sql").write_text("-- first")
        # Non-.sql files must be ignored.
        (sql_dir / "README.md").write_text("docs")

        steps = [PostMigrateStep(sql_dir=sql_dir, sql_file=None, on_error="halt")]
        runner = _runner_ok()

        run_post_migrate_steps(
            steps,
            database_url="postgresql://u@h/db",
            runner=runner,
        )

        called_paths = [call.args[0][-1] for call in runner.run.call_args_list]
        assert called_paths == [
            str(sql_dir / "10_revoke.sql"),
            str(sql_dir / "20_grants.sql"),
        ]

    def test_empty_dir_is_a_noop(self, tmp_path):
        sql_dir = tmp_path / "grants"
        sql_dir.mkdir()
        steps = [PostMigrateStep(sql_dir=sql_dir, sql_file=None, on_error="halt")]
        runner = _runner_ok()
        run_post_migrate_steps(
            steps,
            database_url="postgresql://u@h/db",
            runner=runner,
        )
        runner.run.assert_not_called()


class TestRunPostMigrateStepsOnError:
    def test_halt_raises_deployment_error_on_psql_failure(self, tmp_path):
        sql = tmp_path / "fail.sql"
        sql.write_text("SELECT 1/0;")
        steps = [PostMigrateStep(sql_dir=None, sql_file=sql, on_error="halt")]
        runner = MagicMock()
        runner.run.side_effect = subprocess.CalledProcessError(
            returncode=3, cmd=["psql"], stderr="ERROR: syntax error"
        )

        with pytest.raises(DeploymentError, match=r"fail\.sql"):
            run_post_migrate_steps(
                steps,
                database_url="postgresql://u@h/db",
                runner=runner,
            )

    def test_halt_stops_after_first_failure(self, tmp_path):
        bad = tmp_path / "10_bad.sql"
        bad.write_text("bad")
        never_run = tmp_path / "20_never.sql"
        never_run.write_text("good")
        steps = [
            PostMigrateStep(sql_dir=None, sql_file=bad, on_error="halt"),
            PostMigrateStep(sql_dir=None, sql_file=never_run, on_error="halt"),
        ]
        runner = MagicMock()
        runner.run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["psql"], stderr="boom"
        )
        with pytest.raises(DeploymentError):
            run_post_migrate_steps(
                steps,
                database_url="postgresql://u@h/db",
                runner=runner,
            )
        # Only the first file ran.
        assert runner.run.call_count == 1

    def test_warn_logs_and_continues(self, tmp_path, caplog):
        bad = tmp_path / "10_bad.sql"
        bad.write_text("bad")
        good = tmp_path / "20_good.sql"
        good.write_text("good")
        steps = [
            PostMigrateStep(sql_dir=None, sql_file=bad, on_error="warn"),
            PostMigrateStep(sql_dir=None, sql_file=good, on_error="warn"),
        ]
        runner = MagicMock()
        # First call fails, second call succeeds.
        runner.run.side_effect = [
            subprocess.CalledProcessError(
                returncode=1, cmd=["psql"], stderr="WARNING-ish failure"
            ),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ]

        with caplog.at_level(logging.WARNING, logger="fraisier.post_migrate"):
            run_post_migrate_steps(
                steps,
                database_url="postgresql://u@h/db",
                runner=runner,
            )

        assert runner.run.call_count == 2
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("10_bad.sql" in r.getMessage() for r in warnings)


# ---------------------------------------------------------------------------
# Shared orchestration seam (deploy path + `db restore` CLI, #273)
# ---------------------------------------------------------------------------


class TestRunConfiguredPostMigrate:
    def test_skips_when_database_url_missing(self, tmp_path):
        # No database_url and none passed — nothing to connect to.
        sql = tmp_path / "grant.sql"
        sql.write_text("GRANT USAGE ON SCHEMA tenant TO app;")
        runner = _runner_ok()

        run_configured_post_migrate(
            {"post_migrate": [{"sql_file": str(sql)}]},
            app_path=tmp_path,
            runner=runner,
        )

        runner.run.assert_not_called()

    def test_skips_when_no_steps_configured(self):
        # database_url present but the post_migrate list is empty.
        runner = _runner_ok()

        run_configured_post_migrate(
            {"database_url": "postgresql://u@h/db", "post_migrate": []},
            app_path=Path("/srv/api"),
            runner=runner,
        )

        runner.run.assert_not_called()

    def test_runs_configured_steps_with_database_url_from_config(self, tmp_path):
        sql = tmp_path / "grant.sql"
        sql.write_text("GRANT USAGE ON SCHEMA tenant TO app;")
        runner = _runner_ok()

        run_configured_post_migrate(
            {
                "database_url": "postgresql://u@h/db",
                "post_migrate": [{"sql_file": str(sql)}],
            },
            app_path=tmp_path,
            runner=runner,
        )

        runner.run.assert_called_once()
        cmd = runner.run.call_args.args[0]
        assert cmd == [
            "psql",
            "postgresql://u@h/db",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            str(sql),
        ]

    def test_explicit_database_url_overrides_config(self, tmp_path):
        sql = tmp_path / "grant.sql"
        sql.write_text("GRANT USAGE ON SCHEMA tenant TO app;")
        runner = _runner_ok()

        run_configured_post_migrate(
            {
                "database_url": "postgresql://ignored@h/db",
                "post_migrate": [{"sql_file": str(sql)}],
            },
            app_path=tmp_path,
            runner=runner,
            database_url="postgresql://override@h/db",
        )

        cmd = runner.run.call_args.args[0]
        assert cmd[1] == "postgresql://override@h/db"

    def test_halt_failure_propagates_deployment_error(self, tmp_path):
        sql = tmp_path / "grant.sql"
        sql.write_text("boom")
        runner = MagicMock()
        runner.run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["psql"], stderr="permission denied"
        )

        with pytest.raises(DeploymentError):
            run_configured_post_migrate(
                {
                    "database_url": "postgresql://u@h/db",
                    "post_migrate": [{"sql_file": str(sql), "on_error": "halt"}],
                },
                app_path=tmp_path,
                runner=runner,
            )

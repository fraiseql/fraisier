"""Unit tests for preflight helpers and run_migration_preflight orchestration."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.preflight import (
    MigrationPreflightResult,
    _run_confiture_preflight,
    run_migration_preflight,
)
from fraisier.errors import DatabaseError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _proc(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _pf_json(
    issues: list[dict] | None = None, *, ok: bool | None = None, checked: int = 0
) -> str:
    """Build the JSON confiture 0.32 emits for ``migrate preflight --against``.

    Shape: ``{ok, window_safe, summary, issues[]}``. ``ok`` defaults to "no
    error-severity issues"; ``checked`` is ``summary.migrations_checked``.
    """
    issues = issues or []
    errors = sum(1 for i in issues if i.get("severity") == "error")
    warnings = sum(1 for i in issues if i.get("severity") == "warning")
    return json.dumps(
        {
            "ok": (errors == 0) if ok is None else ok,
            "window_safe": True,
            "summary": {
                "errors": errors,
                "warnings": warnings,
                "info": 0,
                "migrations_checked": checked,
                "db_consumed": False,
            },
            "issues": issues,
        }
    )


def _replay_failed(version: str, name: str, error: str) -> dict:
    """A confiture 0.32 PFLIGHT_REPLAY_FAILED error issue."""
    return {
        "severity": "error",
        "code": "PFLIGHT_REPLAY_FAILED",
        "message": (
            f"Migration {version} ({name}) failed to replay against the preflight DB."
        ),
        "migration": version,
        "file": None,
        "line": None,
        "details": {"error": error},
    }


def _non_txn_warning(
    version: str, name: str, stmt: str = "CREATE INDEX CONCURRENTLY: idx"
) -> dict:
    """A confiture 0.32 PFLIGHT_NON_TRANSACTIONAL warning issue."""
    return {
        "severity": "warning",
        "code": "PFLIGHT_NON_TRANSACTIONAL",
        "message": (
            f"Migration {version} ({name}) has non-transactional statement(s): {stmt}."
        ),
        "migration": version,
        "details": {"statements": [stmt]},
    }


# ---------------------------------------------------------------------------
# _run_confiture_preflight — subprocess mock tests for --against
# ---------------------------------------------------------------------------


class TestRunConfiturePreflight:
    _CFG = Path("confiture.yaml")
    _MIGRATIONS = Path("db/migrations")
    _URL = "postgresql://admin@localhost/fraisier_preflight_abc"

    def test_exit_0_all_passed(self):
        # 0.32 enumerates only issues, not passing migrations; success is an
        # empty issue list with the count in summary.migrations_checked.
        stdout = _pf_json([], checked=1)
        with patch("subprocess.run", return_value=_proc(0, stdout)):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert result.all_passed is True
        assert result.failure_count == 0
        assert result.migrations_checked == 1

    def test_exit_7_failures_recorded(self):
        stdout = _pf_json(
            [_replay_failed("001", "bad", "column does not exist")], checked=1
        )
        with patch("subprocess.run", return_value=_proc(7, stdout)):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert result.all_passed is False
        assert result.failure_count == 1
        assert result.failures[0].version == "001"
        assert result.failures[0].name == "bad"
        assert result.failures[0].error == "column does not exist"

    def test_exit_2_raises_database_error(self):
        with (
            patch("subprocess.run", return_value=_proc(2, "", "config error")),
            pytest.raises(DatabaseError, match="exit 2"),
        ):
            _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)

    def test_exit_3_raises_database_error(self):
        with (
            patch("subprocess.run", return_value=_proc(3, "", "fatal")),
            pytest.raises(DatabaseError),
        ):
            _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)

    def test_non_transactional_recorded_as_skipped(self):
        """confiture 0.33 skips a non-transactional migration in preflight — a
        PFLIGHT_NON_TRANSACTIONAL warning and exit 0, not a replay failure
        (#169). fraisier records it as a skipped check, never a block."""
        stdout = _pf_json([_non_txn_warning("001", "conc_idx")], checked=1)
        with patch("subprocess.run", return_value=_proc(0, stdout)):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert result.all_passed is True
        assert result.failure_count == 0
        assert any(m.skipped for m in result.migrations)

    def test_executable_resolved_from_venv(self):
        """confiture is resolved relative to sys.executable, not as a bare name."""
        with patch("subprocess.run", return_value=_proc(0, _pf_json())) as mock_run:
            _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        cmd = mock_run.call_args[0][0]
        expected_exe = str(Path(sys.executable).parent / "confiture")
        assert cmd[0] == expected_exe

    def test_command_includes_against_flag(self):
        with patch("subprocess.run", return_value=_proc(0, _pf_json())) as mock_run:
            _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        cmd = mock_run.call_args[0][0]
        assert "--against" in cmd
        assert self._URL in cmd

    def test_command_includes_config_flag(self):
        with patch("subprocess.run", return_value=_proc(0, _pf_json())) as mock_run:
            _run_confiture_preflight(Path("custom.yaml"), self._MIGRATIONS, self._URL)
        cmd = mock_run.call_args[0][0]
        assert "--config" in cmd
        assert "custom.yaml" in cmd

    def test_command_omits_config_when_none(self):
        with patch("subprocess.run", return_value=_proc(0, _pf_json())) as mock_run:
            _run_confiture_preflight(None, self._MIGRATIONS, self._URL)
        cmd = mock_run.call_args[0][0]
        assert "--config" not in cmd

    def test_command_includes_since_when_set(self):
        with patch("subprocess.run", return_value=_proc(0, _pf_json())) as mock_run:
            _run_confiture_preflight(
                None, self._MIGRATIONS, self._URL, since="00000000000000"
            )
        cmd = mock_run.call_args[0][0]
        assert "--since" in cmd
        assert "00000000000000" in cmd

    def test_command_includes_migrations_dir(self):
        with patch("subprocess.run", return_value=_proc(0, _pf_json())) as mock_run:
            _run_confiture_preflight(self._CFG, Path("custom/migrations"), self._URL)
        cmd = mock_run.call_args[0][0]
        assert "--migrations-dir" in cmd
        assert "custom/migrations" in cmd

    def test_command_includes_format_json(self):
        with patch("subprocess.run", return_value=_proc(0, _pf_json())) as mock_run:
            _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        cmd = mock_run.call_args[0][0]
        assert "--format" in cmd
        assert "json" in cmd

    def test_empty_migrations_list(self):
        with patch("subprocess.run", return_value=_proc(0, _pf_json())):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert result.all_passed is True
        assert result.migrations == []

    def test_timeout_seconds_passed_to_subprocess(self):
        with patch("subprocess.run", return_value=_proc(0, _pf_json())) as mock_run:
            _run_confiture_preflight(
                self._CFG, self._MIGRATIONS, self._URL, timeout_seconds=60
            )
        assert mock_run.call_args.kwargs.get("timeout") == 60

    def test_multiple_failures_recorded(self):
        stdout = _pf_json(
            [
                _replay_failed("002", "bad", "table missing"),
                _replay_failed("004", "worse", "syntax error"),
            ],
            checked=3,
        )
        with patch("subprocess.run", return_value=_proc(7, stdout)):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert result.failure_count == 2
        assert result.migrations_checked == 3
        assert {m.version for m in result.failures} == {"002", "004"}

    # -- #259: a fatal exit must surface confiture's *stdout* diagnostics --
    # confiture writes its structured failure report (issue code, offending
    # migration, the underlying error/path) to stdout; stderr is typically
    # empty.  fraisier used to report only stderr, leaving a bare "exit N".

    _REPLAY_FAILED_STDOUT = json.dumps(
        {
            "ok": False,
            "summary": {"errors": 1, "warnings": 0, "migrations_checked": 1},
            "issues": [
                _replay_failed(
                    "0001",
                    "recreate_widget",
                    "[Errno 2] No such file or directory: "
                    "'/app/db/0_schema/funcs/widget.sql'",
                )
            ],
        }
    )

    def test_replay_failure_surfaces_error_detail(self):
        """An exit-7 replay failure becomes a structured failure carrying the
        offending migration + underlying error (the #259 diagnostic), rather
        than a fatal raise — exit 7 is a valid preflight outcome in 0.32."""
        with patch(
            "subprocess.run",
            return_value=_proc(7, self._REPLAY_FAILED_STDOUT, ""),
        ):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert result.failure_count == 1
        failure = result.failures[0]
        assert failure.version == "0001"
        assert "0_schema/funcs/widget.sql" in (failure.error or "")

    def test_exit_7_without_mappable_migration_raises(self):
        """An exit-7 error-severity issue not tied to a migration cannot be
        represented per-migration → surface the raw diagnostics + skip hint
        rather than silently report 'no failures'."""
        stdout = json.dumps(
            {
                "ok": False,
                "summary": {"errors": 1, "migrations_checked": 0},
                "issues": [
                    {
                        "severity": "error",
                        "code": "PFLIGHT_CONFIG_INVALID",
                        "message": "boom",
                    }
                ],
            }
        )
        with (
            patch("subprocess.run", return_value=_proc(7, stdout)),
            pytest.raises(DatabaseError) as exc_info,
        ):
            _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        msg = str(exc_info.value)
        assert "PFLIGHT_CONFIG_INVALID" in msg
        assert "--skip-preflight" in msg

    def test_fatal_exit_falls_back_to_stderr_when_stdout_empty(self):
        with (
            patch("subprocess.run", return_value=_proc(2, "", "boom on stderr")),
            pytest.raises(DatabaseError, match="exit 2") as exc_info,
        ):
            _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert "boom on stderr" in str(exc_info.value)

    def test_unrecognized_schema_raises_instead_of_silent_pass(self):
        """A result whose schema isn't the 0.30+ {ok,summary,issues} shape (e.g.
        an old 0.9.x against.migrations envelope from a skewed confiture) must
        not be silently treated as 'no failures' — surface the version skew."""
        stdout = json.dumps({"static": {}, "against": {"migrations": []}})
        with (
            patch("subprocess.run", return_value=_proc(0, stdout)),
            pytest.raises(DatabaseError) as exc_info,
        ):
            _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        msg = str(exc_info.value)
        assert "schema" in msg.lower() or "version" in msg.lower()


# ---------------------------------------------------------------------------
# run_migration_preflight — orchestration and cleanup
# ---------------------------------------------------------------------------


_BACKUP = Path("/backups/prod.dump")
_ADMIN_URL = "postgresql://admin@localhost/postgres"


def _make_passing_result() -> MigrationPreflightResult:
    return MigrationPreflightResult(
        migrations=[], schema_extraction_ms=100, total_ms=200
    )


class TestRunMigrationPreflightOrchestration:
    _P_EXTRACT = "fraisier.dbops.preflight.extract_schema_only"
    _P_PREFLIGHT_DB = "fraisier.dbops.preflight.PreflightDatabase"
    _P_CONFITURE = "fraisier.dbops.preflight._run_confiture_preflight"

    def _mock_preflight_db(self) -> MagicMock:
        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.url = "postgresql://admin@localhost/fraisier_preflight_abc"
        return mock_db

    def test_calls_extract_schema_only(self, tmp_path):
        schema_file = tmp_path / "schema.sql"
        schema_file.write_text("CREATE TABLE foo (id INT);")
        mock_db = self._mock_preflight_db()

        with (
            patch(self._P_EXTRACT, return_value=schema_file) as mock_extract,
            patch(self._P_PREFLIGHT_DB, return_value=mock_db),
            patch(self._P_CONFITURE, return_value=_make_passing_result()),
        ):
            run_migration_preflight(
                backup_path=_BACKUP,
                admin_url=_ADMIN_URL,
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        mock_extract.assert_called_once_with(_BACKUP)

    def test_restore_schema_called_with_schema_path(self, tmp_path):
        schema_file = tmp_path / "schema.sql"
        schema_file.write_text("")
        mock_db = self._mock_preflight_db()

        with (
            patch(self._P_EXTRACT, return_value=schema_file),
            patch(self._P_PREFLIGHT_DB, return_value=mock_db),
            patch(self._P_CONFITURE, return_value=_make_passing_result()),
        ):
            run_migration_preflight(
                backup_path=_BACKUP,
                admin_url=_ADMIN_URL,
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        mock_db.restore_schema.assert_called_once_with(schema_file)

    def test_returns_result_with_timing(self, tmp_path):
        schema_file = tmp_path / "schema.sql"
        schema_file.write_text("")
        mock_db = self._mock_preflight_db()

        with (
            patch(self._P_EXTRACT, return_value=schema_file),
            patch(self._P_PREFLIGHT_DB, return_value=mock_db),
            patch(self._P_CONFITURE, return_value=_make_passing_result()),
        ):
            result = run_migration_preflight(
                backup_path=_BACKUP,
                admin_url=_ADMIN_URL,
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        assert result.schema_extraction_ms >= 0
        assert result.total_ms >= 0

    def test_schema_file_deleted_after_success(self, tmp_path):
        schema_file = tmp_path / "schema.sql"
        schema_file.write_text("")
        mock_db = self._mock_preflight_db()

        with (
            patch(self._P_EXTRACT, return_value=schema_file),
            patch(self._P_PREFLIGHT_DB, return_value=mock_db),
            patch(self._P_CONFITURE, return_value=_make_passing_result()),
        ):
            run_migration_preflight(
                backup_path=_BACKUP,
                admin_url=_ADMIN_URL,
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        assert not schema_file.exists()

    def test_schema_file_deleted_after_confiture_failure(self, tmp_path):
        schema_file = tmp_path / "schema.sql"
        schema_file.write_text("")
        mock_db = self._mock_preflight_db()

        with (
            patch(self._P_EXTRACT, return_value=schema_file),
            patch(self._P_PREFLIGHT_DB, return_value=mock_db),
            patch(self._P_CONFITURE, side_effect=DatabaseError("confiture failed")),
            pytest.raises(DatabaseError),
        ):
            run_migration_preflight(
                backup_path=_BACKUP,
                admin_url=_ADMIN_URL,
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        assert not schema_file.exists()

    def test_extract_failure_propagates(self, tmp_path):
        with (
            patch(self._P_EXTRACT, side_effect=DatabaseError("pg_restore failed")),
            pytest.raises(DatabaseError, match="pg_restore failed"),
        ):
            run_migration_preflight(
                backup_path=_BACKUP,
                admin_url=_ADMIN_URL,
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

    def test_empty_migrations_returns_all_passed(self, tmp_path):
        schema_file = tmp_path / "schema.sql"
        schema_file.write_text("")
        mock_db = self._mock_preflight_db()
        empty_result = MigrationPreflightResult(migrations=[])

        with (
            patch(self._P_EXTRACT, return_value=schema_file),
            patch(self._P_PREFLIGHT_DB, return_value=mock_db),
            patch(self._P_CONFITURE, return_value=empty_result),
        ):
            result = run_migration_preflight(
                backup_path=_BACKUP,
                admin_url=_ADMIN_URL,
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        assert result.all_passed is True
        assert result.migrations == []

    def test_empty_ledger_with_populated_schema_aborts(self, tmp_path):
        """#262: tracking table present but its restored ledger is empty, while
        the restored schema already holds application objects → abort, and never
        invoke confiture to replay the already-applied history."""
        schema_file = tmp_path / "schema.sql"
        schema_file.write_text("")
        mock_db = self._mock_preflight_db()
        mock_db.has_table.return_value = True
        mock_db.count_table_rows.return_value = 0  # empty ledger
        mock_db.count_user_relations.return_value = 7  # populated schema

        with (
            patch(self._P_EXTRACT, return_value=schema_file),
            patch(self._P_PREFLIGHT_DB, return_value=mock_db),
            patch(self._P_CONFITURE) as mock_confiture,
            pytest.raises(DatabaseError, match=r"262|0 rows"),
        ):
            run_migration_preflight(
                backup_path=_BACKUP,
                admin_url=_ADMIN_URL,
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        # The already-applied migrations must NOT be replayed.
        mock_confiture.assert_not_called()

    def test_empty_ledger_with_empty_schema_proceeds(self, tmp_path):
        """A genuinely fresh backup (empty ledger AND no application objects) is
        not a #262 case — replaying all is safe, so the guard stays out of the
        way and confiture runs."""
        schema_file = tmp_path / "schema.sql"
        schema_file.write_text("")
        mock_db = self._mock_preflight_db()
        mock_db.has_table.return_value = True
        mock_db.count_table_rows.return_value = 0  # empty ledger
        mock_db.count_user_relations.return_value = 0  # nothing to collide with

        with (
            patch(self._P_EXTRACT, return_value=schema_file),
            patch(self._P_PREFLIGHT_DB, return_value=mock_db),
            patch(self._P_CONFITURE, return_value=_make_passing_result()) as mock_cf,
        ):
            run_migration_preflight(
                backup_path=_BACKUP,
                admin_url=_ADMIN_URL,
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        mock_cf.assert_called_once()


# ---------------------------------------------------------------------------
# PreflightDatabase — DROP failure is best-effort (no second exception)
# ---------------------------------------------------------------------------


class TestPreflightDatabaseDropFailure:
    def test_drop_failure_does_not_suppress_original_exception(self):
        """If both the with-block and _drop() raise, original exception wins."""
        from fraisier.dbops.preflight import PreflightDatabase

        def boom_drop(self_db: PreflightDatabase) -> None:
            raise RuntimeError("drop failed")

        with (
            patch.object(PreflightDatabase, "_create"),
            patch.object(PreflightDatabase, "_drop", boom_drop),
            pytest.raises(ValueError, match="original error"),
            PreflightDatabase(admin_url="postgresql://admin@localhost/postgres"),
        ):
            raise ValueError("original error")

    def test_drop_always_attempted_on_clean_exit(self):
        from fraisier.dbops.preflight import PreflightDatabase

        drop_called = False

        def track_drop(self_db: PreflightDatabase) -> None:
            nonlocal drop_called
            drop_called = True

        with (
            patch.object(PreflightDatabase, "_create"),
            patch.object(PreflightDatabase, "_drop", track_drop),
            PreflightDatabase(admin_url="postgresql://admin@localhost/postgres"),
        ):
            pass

        assert drop_called

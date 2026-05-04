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


def _against_json(migrations: list[dict]) -> str:
    """Build the JSON envelope confiture emits when --against is used."""
    return json.dumps(
        {
            "static": {},
            "against": {
                "against_url": "postgresql://admin@localhost/fraisier_preflight_abc",
                "migrations": migrations,
            },
        }
    )


def _mig(
    version: str,
    name: str,
    success: bool,
    *,
    error: str | None = None,
    skipped: bool = False,
    skipped_reason: str | None = None,
    time_ms: int = 50,
) -> dict:
    m: dict = {
        "version": version,
        "name": name,
        "success": success,
        "execution_time_ms": time_ms,
    }
    if error is not None:
        m["error"] = error
    if skipped:
        m["skipped"] = True
    if skipped_reason is not None:
        m["skipped_reason"] = skipped_reason
    return m


# ---------------------------------------------------------------------------
# _run_confiture_preflight — subprocess mock tests for --against
# ---------------------------------------------------------------------------


class TestRunConfiturePreflight:
    _CFG = Path("confiture.yaml")
    _MIGRATIONS = Path("db/migrations")
    _URL = "postgresql://admin@localhost/fraisier_preflight_abc"

    def test_exit_0_all_passed(self):
        stdout = _against_json([_mig("001", "ok", True)])
        with patch("subprocess.run", return_value=_proc(0, stdout)):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert result.all_passed is True
        assert len(result.migrations) == 1
        assert result.migrations[0].version == "001"
        assert result.migrations[0].passed is True

    def test_exit_1_failures_recorded(self):
        stdout = _against_json(
            [_mig("001", "bad", False, error="column does not exist")]
        )
        with patch("subprocess.run", return_value=_proc(1, stdout)):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert result.all_passed is False
        assert result.failure_count == 1
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

    def test_against_envelope_key_extracted(self):
        """Output with "against" key uses the against sub-object."""
        migrations = [_mig("001", "ok", True)]
        stdout = json.dumps({"static": {}, "against": {"migrations": migrations}})
        with patch("subprocess.run", return_value=_proc(0, stdout)):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert len(result.migrations) == 1

    def test_flat_envelope_fallback(self):
        """Output without "against" key falls back to root data."""
        migrations = [_mig("001", "ok", True)]
        stdout = json.dumps({"migrations": migrations})
        with patch("subprocess.run", return_value=_proc(0, stdout)):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert len(result.migrations) == 1

    def test_skipped_migration_recorded(self):
        migrations = [
            _mig(
                "001",
                "skip_me",
                True,
                skipped=True,
                skipped_reason="non-transactional",
            )
        ]
        stdout = _against_json(migrations)
        with patch("subprocess.run", return_value=_proc(0, stdout)):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert result.migrations[0].skipped is True
        assert result.migrations[0].skipped_reason == "non-transactional"

    def test_executable_resolved_from_venv(self):
        """confiture is resolved relative to sys.executable, not as a bare name."""
        stdout = _against_json([])
        with patch("subprocess.run", return_value=_proc(0, stdout)) as mock_run:
            _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        cmd = mock_run.call_args[0][0]
        expected_exe = str(Path(sys.executable).parent / "confiture")
        assert cmd[0] == expected_exe

    def test_command_includes_against_flag(self):
        stdout = _against_json([])
        with patch("subprocess.run", return_value=_proc(0, stdout)) as mock_run:
            _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        cmd = mock_run.call_args[0][0]
        assert "--against" in cmd
        assert self._URL in cmd

    def test_command_includes_config_flag(self):
        stdout = _against_json([])
        with patch("subprocess.run", return_value=_proc(0, stdout)) as mock_run:
            _run_confiture_preflight(Path("custom.yaml"), self._MIGRATIONS, self._URL)
        cmd = mock_run.call_args[0][0]
        assert "--config" in cmd
        assert "custom.yaml" in cmd

    def test_command_includes_migrations_dir(self):
        stdout = _against_json([])
        with patch("subprocess.run", return_value=_proc(0, stdout)) as mock_run:
            _run_confiture_preflight(self._CFG, Path("custom/migrations"), self._URL)
        cmd = mock_run.call_args[0][0]
        assert "--migrations-dir" in cmd
        assert "custom/migrations" in cmd

    def test_command_includes_format_json(self):
        stdout = _against_json([])
        with patch("subprocess.run", return_value=_proc(0, stdout)) as mock_run:
            _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        cmd = mock_run.call_args[0][0]
        assert "--format" in cmd
        assert "json" in cmd

    def test_empty_migrations_list(self):
        stdout = _against_json([])
        with patch("subprocess.run", return_value=_proc(0, stdout)):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert result.all_passed is True
        assert result.migrations == []

    def test_timeout_seconds_passed_to_subprocess(self):
        stdout = _against_json([])
        with patch("subprocess.run", return_value=_proc(0, stdout)) as mock_run:
            _run_confiture_preflight(
                self._CFG, self._MIGRATIONS, self._URL, timeout_seconds=60
            )
        assert mock_run.call_args.kwargs.get("timeout") == 60

    def test_multiple_migrations_mixed_results(self):
        migrations = [
            _mig("001", "ok", True),
            _mig("002", "bad", False, error="table missing"),
            _mig("003", "also_ok", True),
        ]
        stdout = _against_json(migrations)
        with patch("subprocess.run", return_value=_proc(1, stdout)):
            result = _run_confiture_preflight(self._CFG, self._MIGRATIONS, self._URL)
        assert result.failure_count == 1
        assert len(result.migrations) == 3
        assert result.failures[0].version == "002"


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

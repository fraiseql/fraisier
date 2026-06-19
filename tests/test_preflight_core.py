"""Unit tests for fraisier.dbops.preflight — Phase 1: core primitives."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.preflight import (
    MigrationCheck,
    MigrationPreflightResult,
    PreflightDatabase,
    extract_schema_only,
)
from fraisier.errors import DatabaseError

# ---------------------------------------------------------------------------
# extract_schema_only
# ---------------------------------------------------------------------------


class TestExtractSchemaOnly:
    def _ok(self) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def _fail(
        self, stderr: str = "pg_restore error"
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=stderr
        )

    def test_returns_path_with_sql_suffix(self, tmp_path):
        backup = tmp_path / "mydb.dump"
        backup.write_bytes(b"fake backup content")

        with patch("subprocess.run", return_value=self._ok()) as mock_run:
            # Create the schema file so the path exists
            schema_file = tmp_path / "mydb_schema.sql"
            schema_file.write_text("CREATE TABLE foo (id INT);")

            result = extract_schema_only(backup, output_dir=tmp_path)

        assert result.suffix == ".sql"
        assert result.stem == "mydb_schema"
        mock_run.assert_called_once()

    def test_uses_pg_restore_schema_only_flags(self, tmp_path):
        backup = tmp_path / "backup.dump"
        backup.write_bytes(b"content")

        with patch("subprocess.run", return_value=self._ok()) as mock_run:
            extract_schema_only(backup, output_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        assert "pg_restore" in cmd
        assert "--schema-only" in cmd
        assert "--no-owner" in cmd
        assert "--no-acl" in cmd

    def test_raises_database_error_on_failure(self, tmp_path):
        backup = tmp_path / "bad.dump"
        backup.write_bytes(b"content")

        with (
            patch(
                "subprocess.run", return_value=self._fail("pg_restore: invalid archive")
            ),
            pytest.raises(DatabaseError, match="Schema extraction failed"),
        ):
            extract_schema_only(backup, output_dir=tmp_path)

    def test_creates_temp_dir_when_output_dir_none(self, tmp_path):
        backup = tmp_path / "backup.dump"
        backup.write_bytes(b"content")

        with (
            patch("subprocess.run", return_value=self._ok()),
            patch("tempfile.mkdtemp", return_value=str(tmp_path)) as mock_mkdtemp,
        ):
            extract_schema_only(backup)

        mock_mkdtemp.assert_called_once()
        assert "fraisier_preflight_" in mock_mkdtemp.call_args[1]["prefix"]

    def test_passes_backup_path_to_command(self, tmp_path):
        backup = tmp_path / "production.dump"
        backup.write_bytes(b"content")

        with patch("subprocess.run", return_value=self._ok()) as mock_run:
            extract_schema_only(backup, output_dir=tmp_path)

        cmd = mock_run.call_args[0][0]
        assert str(backup) in cmd


# ---------------------------------------------------------------------------
# PreflightDatabase
# ---------------------------------------------------------------------------


class TestPreflightDatabase:
    def _make_db(
        self, admin_url: str = "postgresql://admin@localhost/postgres"
    ) -> PreflightDatabase:
        return PreflightDatabase(admin_url=admin_url)

    def test_db_name_has_correct_prefix(self):
        db = self._make_db()
        assert db.db_name.startswith("fraisier_preflight_")

    def test_db_name_is_randomized(self):
        db1 = self._make_db()
        db2 = self._make_db()
        assert db1.db_name != db2.db_name

    def test_db_name_suffix_is_8_hex_chars(self):
        db = self._make_db()
        suffix = db.db_name.removeprefix("fraisier_preflight_")
        assert len(suffix) == 8
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_create_calls_create_database(self):
        db = self._make_db()
        with patch("fraisier.dbops.preflight._run_admin_sql") as mock_sql:
            mock_sql.return_value = subprocess.CompletedProcess([], 0, "", "")
            with patch("fraisier.dbops.preflight._terminate_preflight_connections"):
                db._create()

        call_args = mock_sql.call_args_list
        create_call = call_args[0]
        assert f"CREATE DATABASE {db.db_name}" in create_call[0][1]

    def test_create_raises_on_failure(self):
        db = self._make_db()
        fail_result = subprocess.CompletedProcess([], 1, "", "permission denied")
        with (
            patch("fraisier.dbops.preflight._run_admin_sql", return_value=fail_result),
            pytest.raises(DatabaseError, match="Failed to create preflight database"),
        ):
            db._create()

    def test_drop_terminates_connections_before_drop(self):
        db = self._make_db()
        call_order: list[str] = []

        def fake_terminate(admin_url: str, db_name: str) -> None:
            call_order.append("terminate")

        def fake_sql(admin_url: str, sql: str) -> subprocess.CompletedProcess[str]:
            call_order.append("sql")
            return subprocess.CompletedProcess([], 0, "", "")

        with (
            patch(
                "fraisier.dbops.preflight._terminate_preflight_connections",
                fake_terminate,
            ),
            patch("fraisier.dbops.preflight._run_admin_sql", fake_sql),
        ):
            db._drop()

        assert call_order == ["terminate", "sql"]

    def test_context_manager_drops_on_exception(self):
        """Temp DB is dropped even when the with-block raises."""
        admin_url = "postgresql://admin@localhost/postgres"
        drop_called = False

        def fake_drop(self_db: PreflightDatabase) -> None:
            nonlocal drop_called
            drop_called = True

        with (
            patch.object(PreflightDatabase, "_create"),
            patch.object(PreflightDatabase, "_drop", fake_drop),
            pytest.raises(ValueError),
            PreflightDatabase(admin_url=admin_url),
        ):
            raise ValueError("simulated failure")

        assert drop_called

    def test_url_set_after_create(self):
        db = self._make_db("postgresql://admin@localhost/postgres")
        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch("fraisier.dbops.preflight._run_admin_sql", return_value=ok):
            db._create()

        assert db.db_name in db.url

    def test_restore_schema_calls_psql(self, tmp_path):
        schema_path = tmp_path / "schema.sql"
        schema_path.write_text("CREATE TABLE foo (id INT);")

        db = self._make_db()
        db.url = "postgresql://admin@localhost/fraisier_preflight_abc"

        ok = subprocess.CompletedProcess([], 0, "", "")
        with patch("subprocess.run", return_value=ok) as mock_run:
            db.restore_schema(schema_path)

        cmd = mock_run.call_args[0][0]
        assert "psql" in cmd
        assert str(schema_path) in cmd

    def test_restore_schema_raises_on_failure(self, tmp_path):
        schema_path = tmp_path / "schema.sql"
        schema_path.write_text("BAD SQL;")

        db = self._make_db()
        db.url = "postgresql://admin@localhost/fraisier_preflight_abc"

        fail = subprocess.CompletedProcess([], 1, "", "syntax error")
        with (
            patch("subprocess.run", return_value=fail),
            pytest.raises(DatabaseError, match="Schema restore"),
        ):
            db.restore_schema(schema_path)


# ---------------------------------------------------------------------------
# MigrationCheck + MigrationPreflightResult
# ---------------------------------------------------------------------------


class TestMigrationCheck:
    def test_defaults(self):
        check = MigrationCheck(version="001", name="add_table", passed=True)
        assert check.error is None
        assert check.time_ms == 0
        assert check.skipped is False
        assert check.skipped_reason is None

    def test_failed_check(self):
        check = MigrationCheck(
            version="002",
            name="bad_migration",
            passed=False,
            error="column does not exist",
        )
        assert check.passed is False
        assert check.error == "column does not exist"


class TestMigrationPreflightResult:
    def test_all_passed_true_when_all_pass(self):
        result = MigrationPreflightResult(
            migrations=[
                MigrationCheck(version="001", name="a", passed=True, time_ms=120),
                MigrationCheck(version="002", name="b", passed=True, time_ms=80),
            ],
            schema_extraction_ms=1200,
            total_ms=1500,
        )
        assert result.all_passed is True
        assert result.failure_count == 0
        assert result.failures == []

    def test_all_passed_false_when_any_fail(self):
        result = MigrationPreflightResult(
            migrations=[
                MigrationCheck(version="001", name="a", passed=True, time_ms=120),
                MigrationCheck(
                    version="002",
                    name="b",
                    passed=False,
                    error="cannot change name of input parameter",
                ),
            ],
            schema_extraction_ms=1200,
            total_ms=1500,
        )
        assert result.all_passed is False
        assert result.failure_count == 1
        assert result.failures[0].version == "002"

    def test_skipped_migrations_do_not_count_as_failures(self):
        result = MigrationPreflightResult(
            migrations=[
                MigrationCheck(version="001", name="ok", passed=True),
                MigrationCheck(
                    version="002",
                    name="concurrent",
                    passed=False,
                    skipped=True,
                    skipped_reason="non-transactional",
                ),
            ]
        )
        assert result.all_passed is True
        assert result.failure_count == 0

    def test_empty_migrations_all_passed(self):
        result = MigrationPreflightResult(migrations=[])
        assert result.all_passed is True
        assert result.failure_count == 0


class TestSuspectedFalsePositive:
    """Issue #250: a later migration failing only because an earlier
    non-transactional migration was skipped is a false alarm, not a real bug."""

    def _skipped_then_dependent_failure(self) -> MigrationPreflightResult:
        return MigrationPreflightResult(
            migrations=[
                MigrationCheck(
                    version="20240101120000",
                    name="add_enum_value",
                    passed=False,
                    skipped=True,
                    skipped_reason="non-transactional: cannot run inside SAVEPOINT",
                ),
                MigrationCheck(
                    version="20240101130000",
                    name="use_enum_value",
                    passed=False,
                    error='relation "public.gizmos" does not exist',
                ),
            ]
        )

    def test_skipped_migrations_collected(self):
        result = self._skipped_then_dependent_failure()
        assert [m.version for m in result.skipped_migrations] == ["20240101120000"]

    def test_dependent_failure_flagged_as_suspected_false_positive(self):
        result = self._skipped_then_dependent_failure()
        suspected = result.suspected_false_positive_failures
        assert [m.version for m in suspected] == ["20240101130000"]

    def test_note_present_and_names_skip_preflight(self):
        note = self._skipped_then_dependent_failure().false_positive_note
        assert note is not None
        assert "--skip-preflight" in note
        assert "non-transactional" in note

    def test_no_false_positive_without_a_skip(self):
        """A missing-object error with no skipped migration is a genuine failure."""
        result = MigrationPreflightResult(
            migrations=[
                MigrationCheck(version="001", name="ok", passed=True),
                MigrationCheck(
                    version="002",
                    name="typo",
                    passed=False,
                    error='relation "typoo" does not exist',
                ),
            ]
        )
        assert result.suspected_false_positive_failures == []
        assert result.false_positive_note is None

    def test_failure_before_skip_not_flagged(self):
        """Only failures *after* the skipped version match the dependency direction."""
        result = MigrationPreflightResult(
            migrations=[
                MigrationCheck(
                    version="20240101120000",
                    name="earlier_failure",
                    passed=False,
                    error='relation "unrelated" does not exist',
                ),
                MigrationCheck(
                    version="20240101130000",
                    name="skipped_concurrent",
                    passed=False,
                    skipped=True,
                    skipped_reason="non-transactional",
                ),
            ]
        )
        assert result.suspected_false_positive_failures == []

    def test_non_missing_object_failure_not_flagged(self):
        """A skip plus an unrelated (non does-not-exist) failure is not a false positive."""
        result = MigrationPreflightResult(
            migrations=[
                MigrationCheck(
                    version="20240101120000",
                    name="skipped_concurrent",
                    passed=False,
                    skipped=True,
                    skipped_reason="non-transactional",
                ),
                MigrationCheck(
                    version="20240101130000",
                    name="syntax_error",
                    passed=False,
                    error="syntax error at or near FROM",
                ),
            ]
        )
        assert result.suspected_false_positive_failures == []
        assert result.false_positive_note is None

"""Tests for fraisier.dbops.restore module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.restore import (
    find_latest_backup,
    restore_backup,
    validate_table_count,
)

_TEST_URL = "postgresql://postgres:pass@localhost:5432/postgres"


class TestRestoreBackup:
    """Test restore_backup."""

    def test_restore_success(self):
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert result.success is True
        assert result.error == ""
        mock_cmd.assert_called_once()
        cmd = mock_cmd.call_args[0][0]
        assert "pg_restore" in cmd
        assert "staging" in cmd
        assert "/backups/prod.dump" in cmd
        assert "--no-owner" in cmd
        assert "--no-acl" in cmd
        assert mock_cmd.call_args.kwargs["connection_url"] == _TEST_URL

    def test_restore_requires_connection_url(self):
        with pytest.raises(TypeError):
            restore_backup(  # ty: ignore[missing-argument]
                backup_path="/backups/prod.dump", db_name="staging"
            )

    def test_restore_failure(self):
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (1, "", "pg_restore: error")
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert result.success is False
        assert "pg_restore: error" in result.error

    def test_restore_backup_accepts_directory_dump_path(self, tmp_path: Path):
        """restore_backup forwards a directory-format dump path to pg_restore unchanged.

        pg_restore auto-detects ``-Fd`` from the positional path being a
        directory. Lock-in for #202 Phase 3 — once parallel pg_dump
        starts producing directory dumps, every existing restore caller
        must work without further changes.
        """
        dir_dump = tmp_path / "mydb_full_20260520_1200_zstd.dump"
        dir_dump.mkdir()
        (dir_dump / "toc.dat").write_text("stub toc")

        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            result = restore_backup(
                backup_path=str(dir_dump),
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert result.success is True
        cmd = mock_cmd.call_args[0][0]
        assert cmd[0] == "pg_restore"
        assert str(dir_dump) in cmd
        assert not any(arg.startswith("-F") for arg in cmd), (
            "pg_restore must auto-detect dump format; no -F flag override"
        )

    def test_restore_with_owner_fix(self):
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                db_owner="appuser",
                connection_url=_TEST_URL,
            )

        assert result.success is True
        # Two calls: pg_restore + REASSIGN OWNED
        assert mock_cmd.call_count == 2
        reassign_cmd = mock_cmd.call_args_list[1][0][0]
        assert "psql" in reassign_cmd
        assert any("REASSIGN OWNED" in arg for arg in reassign_cmd)
        assert any("appuser" in arg for arg in reassign_cmd)

    def test_restore_owner_fix_failure_reported(self):
        """REASSIGN OWNED BY failure sets success=False with error."""
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            # pg_restore succeeds, REASSIGN fails
            mock_cmd.side_effect = [
                (0, "", ""),
                (1, "", "ERROR: role does not exist"),
            ]
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                db_owner="baduser",
                connection_url=_TEST_URL,
            )

        assert result.success is False
        assert "baduser" in result.error or "role" in result.error

    def test_restore_with_jobs(self):
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
                jobs=4,
            )

        assert result.success is True
        cmd = mock_cmd.call_args[0][0]
        assert "-j" in cmd
        j_idx = cmd.index("-j")
        assert cmd[j_idx + 1] == "4"

    def test_restore_jobs_1_no_flag(self):
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
                jobs=1,
            )

        cmd = mock_cmd.call_args[0][0]
        assert "-j" not in cmd

    def test_restore_default_no_j_flag(self):
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        cmd = mock_cmd.call_args[0][0]
        assert "-j" not in cmd

    def test_restore_returns_duration(self):
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert result.duration_seconds > 0

    def test_restore_failure_still_has_duration(self):
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (1, "", "pg_restore: error")
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                connection_url=_TEST_URL,
            )

        assert not result.success
        assert result.duration_seconds > 0

    def test_restore_rejects_bad_db_name(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            restore_backup(
                backup_path="/backups/prod.dump",
                db_name="bad name!",
                connection_url=_TEST_URL,
            )

    def test_restore_rejects_bad_owner(self):
        with pytest.raises(ValueError, match="Invalid database owner"):
            restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                db_owner="bad;owner",
                connection_url=_TEST_URL,
            )


class TestValidateTableCount:
    """Test validate_table_count."""

    def test_validate_table_count_pass(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="75\n", stderr="")
            ok, count = validate_table_count(
                "staging", min_threshold=50, connection_url=_TEST_URL
            )

        assert ok is True
        assert count == 75

    def test_validate_table_count_fail(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="10\n", stderr="")
            ok, count = validate_table_count(
                "staging", min_threshold=50, connection_url=_TEST_URL
            )

        assert ok is False
        assert count == 10


class TestFindLatestBackup:
    """Test find_latest_backup."""

    def test_find_latest_backup(self, tmp_path: Path):
        older = tmp_path / "prod_full_20250101.dump"
        older.write_text("old")

        newer = tmp_path / "prod_full_20250320.dump"
        newer.write_text("new")

        # Ensure distinct mtimes
        import os

        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))

        result = find_latest_backup(tmp_path)
        assert result == newer

    def test_find_latest_backup_empty(self, tmp_path: Path):
        result = find_latest_backup(tmp_path)
        assert result is None

    def test_find_latest_backup_preferred_compression(self, tmp_path: Path):
        """With preferred_compression='lz4', returns newest lz4 dump even if
        a newer zstd dump exists."""
        import os

        zstd_old = tmp_path / "db_full_20260501_0000_zstd.dump"
        zstd_old.write_text("old zstd")
        os.utime(zstd_old, (1000, 1000))

        lz4_mid = tmp_path / "db_full_20260502_0000_lz4.dump"
        lz4_mid.write_text("mid lz4")
        os.utime(lz4_mid, (2000, 2000))

        zstd_new = tmp_path / "db_full_20260503_0000_zstd.dump"
        zstd_new.write_text("new zstd")
        os.utime(zstd_new, (3000, 3000))

        result = find_latest_backup(tmp_path, preferred_compression="lz4")
        assert result == lz4_mid

    def test_find_latest_backup_preferred_compression_fallback(self, tmp_path: Path):
        """When no dump matches the preferred compression, fall back to newest."""
        import os

        zstd = tmp_path / "db_full_20260501_0000_zstd.dump"
        zstd.write_text("zstd")
        os.utime(zstd, (1000, 1000))

        result = find_latest_backup(tmp_path, preferred_compression="lz4")
        assert result == zstd

    def test_find_latest_backup_preferred_compression_none(self, tmp_path: Path):
        """No preference returns newest regardless of compression."""
        import os

        lz4 = tmp_path / "db_full_20260501_0000_lz4.dump"
        lz4.write_text("lz4")
        os.utime(lz4, (1000, 1000))

        zstd = tmp_path / "db_full_20260502_0000_zstd.dump"
        zstd.write_text("zstd")
        os.utime(zstd, (2000, 2000))

        result = find_latest_backup(tmp_path)
        assert result == zstd

    def test_find_latest_backup_returns_directory_dump(self, tmp_path: Path):
        """find_latest_backup discovers pg_dump -Fd directory dumps (#202 Phase 2).

        Parallel pg_dump produces a directory containing toc.dat + per-table
        .dat files. Path.glob matches both files and directories, so the
        function should locate it without special-casing.
        """
        import os

        dir_dump = tmp_path / "mydb_full_20260520_1200_zstd.dump"
        dir_dump.mkdir()
        (dir_dump / "toc.dat").write_text("stub toc")
        os.utime(dir_dump, (2000, 2000))

        result = find_latest_backup(tmp_path)
        assert result == dir_dump
        assert result is not None and result.is_dir()

    def test_find_latest_backup_picks_newer_directory_over_older_file(
        self, tmp_path: Path
    ):
        """When a directory dump is newer than the latest file dump, it wins."""
        import os

        file_dump = tmp_path / "mydb_full_20260519_1200_zstd.dump"
        file_dump.write_text("file dump bytes")
        os.utime(file_dump, (1000, 1000))

        dir_dump = tmp_path / "mydb_full_20260520_1200_zstd.dump"
        dir_dump.mkdir()
        (dir_dump / "toc.dat").write_text("stub toc")
        os.utime(dir_dump, (2000, 2000))

        result = find_latest_backup(tmp_path)
        assert result == dir_dump

    def test_find_latest_backup_picks_newer_file_over_older_directory(
        self, tmp_path: Path
    ):
        """Symmetric case: newer file dump wins over older directory dump."""
        import os

        dir_dump = tmp_path / "mydb_full_20260519_1200_zstd.dump"
        dir_dump.mkdir()
        (dir_dump / "toc.dat").write_text("stub toc")
        os.utime(dir_dump, (1000, 1000))

        file_dump = tmp_path / "mydb_full_20260520_1200_zstd.dump"
        file_dump.write_text("file dump bytes")
        os.utime(file_dump, (2000, 2000))

        result = find_latest_backup(tmp_path)
        assert result == file_dump

    def test_find_latest_backup_preferred_compression_matches_directory(
        self, tmp_path: Path
    ):
        """preferred_compression matches directory dumps by filename suffix.

        The producer (Phase 3) names directory dumps `<db>_<mode>_<ts>_<algo>.dump/`,
        so the filename-suffix match used by file dumps works unchanged.
        """
        import os

        zstd_file = tmp_path / "mydb_full_20260520_1300_zstd.dump"
        zstd_file.write_text("zstd file dump")
        os.utime(zstd_file, (2000, 2000))  # newer

        lz4_dir = tmp_path / "mydb_full_20260520_1200_lz4.dump"
        lz4_dir.mkdir()
        (lz4_dir / "toc.dat").write_text("stub toc")
        os.utime(lz4_dir, (1000, 1000))  # older

        result = find_latest_backup(tmp_path, preferred_compression="lz4")
        assert result == lz4_dir

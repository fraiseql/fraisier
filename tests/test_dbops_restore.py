"""Tests for fraisier.dbops.restore module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.restore import (
    find_latest_backup,
    restore_backup,
    validate_table_count,
)


class TestRestoreBackup:
    """Test restore_backup."""

    def test_restore_success(self):
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            result = restore_backup(backup_path="/backups/prod.dump", db_name="staging")

        assert result.success is True
        assert result.error == ""
        mock_cmd.assert_called_once()
        cmd = mock_cmd.call_args[0][0]
        assert "pg_restore" in cmd
        assert "staging" in cmd
        assert "/backups/prod.dump" in cmd
        assert "--no-owner" in cmd
        assert "--no-acl" in cmd

    def test_restore_failure(self):
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (1, "", "pg_restore: error")
            result = restore_backup(backup_path="/backups/prod.dump", db_name="staging")

        assert result.success is False
        assert "pg_restore: error" in result.error

    def test_restore_with_owner_fix(self):
        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            result = restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                db_owner="appuser",
            )

        assert result.success is True
        # Two calls: pg_restore + REASSIGN OWNED
        assert mock_cmd.call_count == 2
        reassign_cmd = mock_cmd.call_args_list[1][0][0]
        assert "psql" in reassign_cmd
        assert any("REASSIGN OWNED" in arg for arg in reassign_cmd)
        assert any("appuser" in arg for arg in reassign_cmd)

    def test_restore_rejects_bad_db_name(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            restore_backup(backup_path="/backups/prod.dump", db_name="bad name!")

    def test_restore_rejects_bad_owner(self):
        with pytest.raises(ValueError, match="Invalid database owner"):
            restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                db_owner="bad;owner",
            )


class TestValidateTableCount:
    """Test validate_table_count."""

    def test_validate_table_count_pass(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="75\n", stderr="")
            ok, count = validate_table_count("staging", min_threshold=50)

        assert ok is True
        assert count == 75

    def test_validate_table_count_fail(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="10\n", stderr="")
            ok, count = validate_table_count("staging", min_threshold=50)

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

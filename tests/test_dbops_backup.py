"""Tests for fraisier.dbops.backup module."""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.backup import (
    check_disk_space,
    cleanup_old_backups,
    run_backup,
)


class TestRunBackup:
    """Test run_backup pg_dump wrapper."""

    def test_backup_full_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_backup(db_name="proddb", output_dir="/backups")

        assert result.success is True
        assert "proddb" in result.backup_path
        assert "full" in result.backup_path
        assert result.backup_path.startswith("/backups/")
        assert result.backup_path.endswith(".dump")
        assert result.error == ""

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "sudo"
        assert "pg_dump" in cmd
        assert "-Fc" in cmd
        assert "proddb" in cmd

    def test_backup_slim_with_exclusions(self):
        excluded = ["large_logs", "audit_trail"]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_backup(
                db_name="proddb",
                output_dir="/backups",
                mode="slim",
                excluded_tables=excluded,
            )

        assert result.success is True
        assert "slim" in result.backup_path
        cmd = mock_run.call_args[0][0]
        # Each excluded table should appear after a -T flag
        t_indices = [i for i, arg in enumerate(cmd) if arg == "-T"]
        assert len(t_indices) == 2
        assert cmd[t_indices[0] + 1] == "large_logs"
        assert cmd[t_indices[1] + 1] == "audit_trail"

    def test_backup_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="pg_dump: connection refused"
            )
            result = run_backup(db_name="proddb", output_dir="/backups")

        assert result.success is False
        assert "connection refused" in result.error

    def test_backup_rejects_bad_db_name(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            run_backup(db_name="db; rm -rf /", output_dir="/backups")


class TestCheckDiskSpace:
    """Test check_disk_space."""

    def test_check_disk_space_sufficient(self):
        # 100 GB free
        usage = MagicMock(free=100 * 1024**3)
        with patch("shutil.disk_usage", return_value=usage):
            assert check_disk_space("/backups", required_gb=50) is True

    def test_check_disk_space_insufficient(self):
        # 5 GB free
        usage = MagicMock(free=5 * 1024**3)
        with patch("shutil.disk_usage", return_value=usage):
            assert check_disk_space("/backups", required_gb=10) is False


class TestCleanupOldBackups:
    """Test cleanup_old_backups."""

    def test_cleanup_old_backups(self, tmp_path: Path):
        # Create old and recent backup files
        old_file = tmp_path / "proddb_full_20250101_0000.dump"
        old_file.write_text("old")
        # Set mtime to 48 hours ago
        old_mtime = time.time() - 48 * 3600
        os.utime(old_file, (old_mtime, old_mtime))

        recent_file = tmp_path / "proddb_full_20250320_1200.dump"
        recent_file.write_text("recent")

        non_dump = tmp_path / "notes.txt"
        non_dump.write_text("keep me")

        removed = cleanup_old_backups(tmp_path, retention_hours=24)

        assert str(old_file) in removed
        assert not old_file.exists()
        assert recent_file.exists()
        assert non_dump.exists()
        assert len(removed) == 1

"""Tests for fraisier.dbops.backup module."""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.backup import (
    _previous_same_mode_backup,
    _verify_backup_toc,
    check_disk_space,
    cleanup_old_backups,
    run_backup,
)

_TEST_URL = "postgresql://app:pass@localhost:5432/proddb"


class TestRunBackup:
    """Test run_backup pg_dump wrapper."""

    def test_backup_full_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_backup(
                db_name="proddb", output_dir="/backups", database_url=_TEST_URL
            )

        assert result.success is True
        assert "proddb" in result.backup_path
        assert "full" in result.backup_path
        assert result.backup_path.startswith("/backups/")
        assert result.backup_path.endswith(".dump")
        assert result.error == ""

        cmd = mock_run.call_args_list[0][0][0]
        assert cmd[0] == "pg_dump"
        assert "sudo" not in cmd
        assert "-Fc" in cmd
        assert "proddb" in cmd

    def test_backup_requires_database_url(self):
        with pytest.raises(TypeError):
            run_backup(  # ty: ignore[missing-argument]
                db_name="proddb", output_dir="/backups"
            )

    def test_backup_slim_with_exclusions(self):
        excluded = ["large_logs", "audit_trail"]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_backup(
                db_name="proddb",
                output_dir="/backups",
                database_url=_TEST_URL,
                mode="slim",
                excluded_tables=excluded,
            )

        assert result.success is True
        assert "slim" in result.backup_path
        cmd = mock_run.call_args_list[0][0][0]
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
            result = run_backup(
                db_name="proddb", output_dir="/backups", database_url=_TEST_URL
            )

        assert result.success is False
        assert "connection refused" in result.error

    def test_backup_filename_includes_lz4(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_backup(
                db_name="proddb",
                output_dir="/backups",
                database_url=_TEST_URL,
                compression="lz4:1",
            )

        assert result.success
        assert "_lz4.dump" in result.backup_path

    def test_backup_filename_includes_zstd(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_backup(
                db_name="proddb",
                output_dir="/backups",
                database_url=_TEST_URL,
                compression="zstd:9",
            )

        assert result.success
        assert "_zstd.dump" in result.backup_path

    def test_backup_filename_no_suffix_for_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_backup(
                db_name="proddb",
                output_dir="/backups",
                database_url=_TEST_URL,
                compression="none",
            )

        assert result.success
        # Should not have _none in filename, just ends with .dump
        assert not result.backup_path.endswith("_none.dump")
        assert result.backup_path.endswith(".dump")

    def test_backup_rejects_bad_db_name(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            run_backup(
                db_name="db; rm -rf /",
                output_dir="/backups",
                database_url=_TEST_URL,
            )

    def test_returns_failure_when_toc_verification_fails(self, tmp_path: Path):
        # pg_dump succeeds (and writes a stub file), pg_restore --list fails.
        def fake_run(cmd, **_kwargs):
            if cmd[0] == "pg_dump":
                # Find -f <path> and write a tiny file there to simulate dump.
                out = cmd[cmd.index("-f") + 1]
                Path(out).write_bytes(b"truncated")
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd[0] == "pg_restore":
                return MagicMock(
                    returncode=1, stdout="", stderr="pg_restore: error reading file"
                )
            raise AssertionError(f"unexpected cmd: {cmd[0]}")

        with patch("subprocess.run", side_effect=fake_run):
            result = run_backup(
                db_name="proddb",
                output_dir=str(tmp_path),
                database_url=_TEST_URL,
            )

        assert result.success is False
        assert "TOC" in result.error

    def test_returns_failure_when_size_under_threshold(self, tmp_path: Path):
        # Stub a prior dump at 1000 bytes; new dump will be 100 bytes (10%).
        prior = tmp_path / "proddb_full_20250101_0000_zstd.dump"
        prior.write_bytes(b"x" * 1000)

        def fake_run(cmd, **_kwargs):
            if cmd[0] == "pg_dump":
                out = cmd[cmd.index("-f") + 1]
                Path(out).write_bytes(b"y" * 100)
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = run_backup(
                db_name="proddb",
                output_dir=str(tmp_path),
                database_url=_TEST_URL,
            )

        assert result.success is False
        assert "sanity" in result.error.lower()

    def test_returns_success_when_size_at_threshold(self, tmp_path: Path):
        # 600 bytes vs 1000-byte prior = 60% — above 50% threshold.
        prior = tmp_path / "proddb_full_20250101_0000_zstd.dump"
        prior.write_bytes(b"x" * 1000)

        def fake_run(cmd, **_kwargs):
            if cmd[0] == "pg_dump":
                out = cmd[cmd.index("-f") + 1]
                Path(out).write_bytes(b"y" * 600)
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = run_backup(
                db_name="proddb",
                output_dir=str(tmp_path),
                database_url=_TEST_URL,
            )

        assert result.success is True

    def test_skips_size_check_when_prior_is_zero_bytes(self, tmp_path: Path):
        # Defensive: avoid div-by-zero / nonsensical ratio when prior is empty.
        prior = tmp_path / "proddb_full_20250101_0000_zstd.dump"
        prior.write_bytes(b"")

        def fake_run(cmd, **_kwargs):
            if cmd[0] == "pg_dump":
                out = cmd[cmd.index("-f") + 1]
                Path(out).write_bytes(b"y" * 500)
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = run_backup(
                db_name="proddb",
                output_dir=str(tmp_path),
                database_url=_TEST_URL,
            )

        assert result.success is True

    def test_failure_leaves_partial_file_on_disk_for_postmortem(self, tmp_path: Path):
        # Operators may want to inspect the rejected dump; do not auto-delete.
        def fake_run(cmd, **_kwargs):
            if cmd[0] == "pg_dump":
                out = cmd[cmd.index("-f") + 1]
                Path(out).write_bytes(b"truncated")
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="corrupt archive")

        with patch("subprocess.run", side_effect=fake_run):
            result = run_backup(
                db_name="proddb",
                output_dir=str(tmp_path),
                database_url=_TEST_URL,
            )

        assert result.success is False
        assert result.backup_path != ""
        assert Path(result.backup_path).exists()

    def test_returns_success_when_toc_passes_first_run(self, tmp_path: Path):
        # No prior backups in dir — size check skipped, TOC passes.
        def fake_run(cmd, **_kwargs):
            if cmd[0] == "pg_dump":
                out = cmd[cmd.index("-f") + 1]
                Path(out).write_bytes(b"x" * 1000)
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = run_backup(
                db_name="proddb",
                output_dir=str(tmp_path),
                database_url=_TEST_URL,
            )

        assert result.success is True
        assert result.error == ""


class TestVerifyBackupToc:
    """Test _verify_backup_toc helper."""

    def test_passes_when_pg_restore_list_succeeds(self):
        with patch("fraisier.dbops.backup._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "; Archive header...\n", "")
            ok, err = _verify_backup_toc(
                "/backups/proddb_full.dump", connection_url=_TEST_URL
            )
        assert ok is True
        assert err == ""

    def test_fails_when_pg_restore_list_errors(self):
        with patch("fraisier.dbops.backup._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (1, "", "pg_restore: error reading file")
            ok, err = _verify_backup_toc(
                "/backups/proddb_full.dump", connection_url=_TEST_URL
            )
        assert ok is False
        assert "error reading file" in err

    def test_calls_pg_restore_with_list_flag(self):
        with patch("fraisier.dbops.backup._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            _verify_backup_toc(
                "/backups/proddb_full.dump", connection_url=_TEST_URL
            )
        cmd = mock_cmd.call_args[0][0]
        assert cmd[0] == "pg_restore"
        assert "--list" in cmd
        assert "/backups/proddb_full.dump" in cmd


class TestPreviousSameModeBackup:
    """Test _previous_same_mode_backup helper."""

    def test_returns_most_recent_excluding_current(self, tmp_path: Path):
        older = tmp_path / "proddb_full_20250101_0000_zstd.dump"
        newer = tmp_path / "proddb_full_20250102_0000_zstd.dump"
        current = tmp_path / "proddb_full_20250103_0000_zstd.dump"
        other_mode = tmp_path / "proddb_slim_20250102_1200_zstd.dump"
        for p in (older, newer, current, other_mode):
            p.write_text("data")
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))
        os.utime(current, (3_000_000, 3_000_000))
        os.utime(other_mode, (2_500_000, 2_500_000))

        prev = _previous_same_mode_backup(
            tmp_path, db_name="proddb", mode="full", current_path=str(current)
        )

        assert prev == newer

    def test_returns_none_when_no_priors(self, tmp_path: Path):
        prev = _previous_same_mode_backup(
            tmp_path, db_name="proddb", mode="full", current_path="/x.dump"
        )
        assert prev is None

    def test_ignores_other_databases(self, tmp_path: Path):
        other = tmp_path / "otherdb_full_20250101_0000.dump"
        other.write_text("data")
        current = tmp_path / "proddb_full_20250102_0000.dump"
        current.write_text("data")

        prev = _previous_same_mode_backup(
            tmp_path, db_name="proddb", mode="full", current_path=str(current)
        )
        assert prev is None


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

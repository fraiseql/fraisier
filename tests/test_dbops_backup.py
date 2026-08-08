"""Tests for fraisier.dbops.backup module."""

import logging
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.archive import ArchiveCheck, ArchiveVerdict
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


class TestRunBackupJobs:
    """Test run_backup parallel/directory-format mode (#202 Phase 3)."""

    def test_jobs_1_uses_Fc_format_and_file_output(self, tmp_path: Path):
        """jobs=1 (default) preserves the single-stream -Fc behaviour byte-for-byte."""
        captured: dict = {}

        def fake_run(cmd, **_kwargs):
            if cmd[0] == "pg_dump":
                captured["cmd"] = list(cmd)
                out = cmd[cmd.index("-f") + 1]
                Path(out).write_bytes(b"x" * 1000)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = run_backup(
                db_name="proddb",
                output_dir=str(tmp_path),
                database_url=_TEST_URL,
                jobs=1,
            )

        assert result.success is True
        cmd = captured["cmd"]
        assert "-Fc" in cmd
        assert "-Fd" not in cmd
        assert "-j" not in cmd
        assert Path(result.backup_path).is_file()
        assert not Path(result.backup_path).is_dir()

    def test_jobs_default_matches_jobs_1(self, tmp_path: Path):
        """Omitting jobs keeps the file-format behaviour."""

        def fake_run(cmd, **_kwargs):
            if cmd[0] == "pg_dump":
                out = cmd[cmd.index("-f") + 1]
                Path(out).write_bytes(b"x" * 1000)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = run_backup(
                db_name="proddb",
                output_dir=str(tmp_path),
                database_url=_TEST_URL,
            )

        assert result.success is True
        assert Path(result.backup_path).is_file()

    def test_jobs_greater_than_1_uses_Fd_format_and_directory_output(
        self, tmp_path: Path
    ):
        """jobs=4 switches to pg_dump -Fd -j 4 writing a directory dump."""
        captured: dict = {}

        def fake_run(cmd, **_kwargs):
            if cmd[0] == "pg_dump":
                captured["cmd"] = list(cmd)
                out = cmd[cmd.index("-f") + 1]
                # pg_dump -Fd creates the directory and writes toc.dat + per-table blobs.
                Path(out).mkdir(parents=False, exist_ok=False)
                (Path(out) / "toc.dat").write_bytes(b"toc" * 100)
                (Path(out) / "1.dat").write_bytes(b"data" * 250)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = run_backup(
                db_name="proddb",
                output_dir=str(tmp_path),
                database_url=_TEST_URL,
                jobs=4,
            )

        assert result.success is True, result.error
        cmd = captured["cmd"]
        assert "-Fd" in cmd
        assert "-Fc" not in cmd
        assert "-j" in cmd
        assert cmd[cmd.index("-j") + 1] == "4"
        path = Path(result.backup_path)
        assert path.is_dir()
        assert path.name.endswith(".dump")
        assert (path / "toc.dat").exists()

    def test_size_check_uses_directory_total_for_directory_dumps(self, tmp_path: Path):
        """Size sanity check sums directory contents recursively (#202 Phase 3).

        Without recursive sizing, a directory dump's bare inode size
        (~4096 bytes) would falsely trigger the size sanity check
        whenever a prior file dump existed of any meaningful size.
        """
        # Prior file dump: 10_000 bytes
        prev = tmp_path / "proddb_full_20260520_1200_zstd.dump"
        prev.write_bytes(b"x" * 10_000)
        os.utime(prev, (1_000_000, 1_000_000))

        def fake_run(cmd, **_kwargs):
            if cmd[0] == "pg_dump":
                out = cmd[cmd.index("-f") + 1]
                Path(out).mkdir(parents=False, exist_ok=False)
                # New directory dump: 8_000 bytes total — 80% of prior, well above threshold.
                (Path(out) / "toc.dat").write_bytes(b"t" * 2_000)
                (Path(out) / "1.dat").write_bytes(b"d" * 6_000)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = run_backup(
                db_name="proddb",
                output_dir=str(tmp_path),
                database_url=_TEST_URL,
                jobs=4,
            )

        assert result.success is True, result.error
        assert Path(result.backup_path).is_dir()

    def test_size_check_fails_when_directory_dump_undersized(self, tmp_path: Path):
        """A directory dump under 50% of the prior total is rejected."""
        prev = tmp_path / "proddb_full_20260520_1200_zstd.dump"
        prev.write_bytes(b"x" * 10_000)
        os.utime(prev, (1_000_000, 1_000_000))

        def fake_run(cmd, **_kwargs):
            if cmd[0] == "pg_dump":
                out = cmd[cmd.index("-f") + 1]
                Path(out).mkdir(parents=False, exist_ok=False)
                # New directory dump: only 1_000 bytes — 10% of prior.
                (Path(out) / "toc.dat").write_bytes(b"t" * 500)
                (Path(out) / "1.dat").write_bytes(b"d" * 500)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = run_backup(
                db_name="proddb",
                output_dir=str(tmp_path),
                database_url=_TEST_URL,
                jobs=4,
            )

        assert result.success is False
        assert "size sanity" in result.error

    def test_jobs_rejects_zero_and_negative(self, tmp_path: Path):
        """jobs must be a positive integer; 0 and negatives raise ValueError."""
        for bad in (0, -1, -10):
            with pytest.raises(ValueError, match="jobs"):
                run_backup(
                    db_name="proddb",
                    output_dir=str(tmp_path),
                    database_url=_TEST_URL,
                    jobs=bad,
                )


class TestVerifyBackupToc:
    """The producing side reads the same seam as the receiving side (#342).

    "What makes a dump valid" had one implementation here and was about to grow
    a second for the restore and prune paths. It stays one: this helper is an
    adapter over `verify_archive`, keeping `run_backup`'s (ok, error) shape.
    """

    def test_delegates_to_the_shared_seam(self):
        with patch("fraisier.dbops.backup.verify_archive") as verify:
            verify.return_value = ArchiveCheck(ArchiveVerdict.VALID, "")
            ok, err = _verify_backup_toc(
                "/backups/proddb_full.dump", connection_url=_TEST_URL
            )
        verify.assert_called_once()
        assert str(verify.call_args[0][0]) == "/backups/proddb_full.dump"
        assert ok is True
        assert err == ""

    def test_invalid_archive_fails_verification(self):
        with patch("fraisier.dbops.backup.verify_archive") as verify:
            verify.return_value = ArchiveCheck(
                ArchiveVerdict.INVALID, "pg_restore: error reading file"
            )
            ok, err = _verify_backup_toc(
                "/backups/proddb_full.dump", connection_url=_TEST_URL
            )
        assert ok is False
        assert "error reading file" in err

    def test_unverifiable_does_not_fail_the_backup(self, caplog):
        """A dump pg_dump just wrote is not condemned by a missing pg_restore.

        Previously this path raised FileNotFoundError out of `run_backup`, so
        a host without the client tools crashed its own backup. It is a warning
        now — but a warning, not silence: the dump went out unverified.
        """
        with (
            patch("fraisier.dbops.backup.verify_archive") as verify,
            caplog.at_level(logging.WARNING, logger="fraisier.dbops.backup"),
        ):
            verify.return_value = ArchiveCheck(
                ArchiveVerdict.UNVERIFIABLE, "pg_restore not found on PATH"
            )
            ok, err = _verify_backup_toc(
                "/backups/proddb_full.dump", connection_url=_TEST_URL
            )
        assert ok is True
        assert err == ""
        assert "pg_restore not found on PATH" in caplog.text

    def test_no_second_pg_restore_list_implementation(self):
        """One place builds that argv. Stated where it can be enforced.

        Two copies is how the producing side ended up TOC-verifying while the
        receiving side verified nothing — the gap #342 was filed for.

        Matches the *argv construction* (a sequence literal carrying both
        tokens), not the string `--list`, which `fraisier sync` also has as a
        click flag.
        """
        import ast

        import fraisier

        package = Path(fraisier.__file__).parent
        offenders = []
        for path in sorted(package.rglob("*.py")):
            if path.name == "archive.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.List | ast.Tuple):
                    continue
                literals = {
                    e.value
                    for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                }
                if "pg_restore" in literals and "--list" in literals:
                    offenders.append(f"{path.relative_to(package)}:{node.lineno}")

        assert not offenders, (
            "a second pg_restore --list call site (use dbops.archive."
            "verify_archive):\n" + "\n".join(offenders)
        )


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

        removed = cleanup_old_backups(tmp_path, retention_hours=24).removed

        assert str(old_file) in removed
        assert not old_file.exists()
        assert recent_file.exists()
        assert non_dump.exists()
        assert len(removed) == 1

    def test_cleanup_removes_old_directory_dumps(self, tmp_path: Path):
        """Old directory-format dumps (#202 Phase 3) are removed via rmtree."""
        old_dir = tmp_path / "proddb_full_20250101_0000_zstd.dump"
        old_dir.mkdir()
        (old_dir / "toc.dat").write_text("toc")
        (old_dir / "1.dat").write_text("data")
        old_mtime = time.time() - 48 * 3600
        os.utime(old_dir, (old_mtime, old_mtime))

        recent_dir = tmp_path / "proddb_full_20250320_1200_zstd.dump"
        recent_dir.mkdir()
        (recent_dir / "toc.dat").write_text("toc")

        removed = cleanup_old_backups(tmp_path, retention_hours=24).removed

        assert str(old_dir) in removed
        assert not old_dir.exists()
        assert recent_dir.exists()
        assert (recent_dir / "toc.dat").exists()

    def test_cleanup_mixed_files_and_directories(self, tmp_path: Path):
        """Mixed file + directory dumps are both eligible for removal."""
        old_mtime = time.time() - 48 * 3600

        old_file = tmp_path / "proddb_full_20250101_0000.dump"
        old_file.write_text("old file")
        os.utime(old_file, (old_mtime, old_mtime))

        old_dir = tmp_path / "proddb_full_20250101_0100_zstd.dump"
        old_dir.mkdir()
        (old_dir / "toc.dat").write_text("toc")
        os.utime(old_dir, (old_mtime, old_mtime))

        new_file = tmp_path / "proddb_full_20250320_1200.dump"
        new_file.write_text("new file")

        removed = cleanup_old_backups(tmp_path, retention_hours=24).removed

        assert sorted(removed) == sorted([str(old_file), str(old_dir)])
        assert not old_file.exists()
        assert not old_dir.exists()
        assert new_file.exists()


def _aged(path: Path, *, hours: float) -> Path:
    """Backdate *path*'s mtime by *hours* and return it."""
    when = time.time() - hours * 3600
    os.utime(path, (when, when))
    return path


def _dump(directory: Path, name: str, *, hours: float) -> Path:
    """Write a file dump named *name*, aged *hours*."""
    path = directory / name
    path.write_text(name)
    return _aged(path, hours=hours)


@pytest.fixture
def dumps_are_valid():
    """The floor consults validity now (#342), so a floor test must declare it.

    Without this the real `pg_restore --list` runs against these stub files and
    calls every one INVALID — so the test would assert all-invalid semantics on
    a machine that has `pg_restore` and all-valid semantics on one that does
    not. An environment-dependent retention test is the argv-dependence flake
    class PR #306 removed; the fix there was to stop depending on the
    environment, not to skip the test.

    Stating it per class also keeps the floor's new precondition visible: these
    tests describe what the floor does *given valid dumps*, which is no longer
    the only case.
    """
    with patch(
        "fraisier.dbops.backup.verify_archive",
        side_effect=lambda _path: ArchiveCheck(ArchiveVerdict.VALID, ""),
    ):
        yield


@pytest.mark.usefixtures("dumps_are_valid")
class TestCleanupKeepMinimum:
    """The floor: a corpus can never be emptied by the age rule alone."""

    def test_keep_minimum_exempts_newest_regardless_of_age(self, tmp_path: Path):
        """The only case that matters: every dump is past the cutoff.

        A stalled producer stops writing; the whole corpus ages out
        together. Exempting before the age test is what keeps the newest
        three alive here — exempting after would keep nothing.
        """
        for index in range(5):
            _dump(tmp_path, f"db_full_{index}.dump", hours=48 + index)

        removed = cleanup_old_backups(
            tmp_path, retention_hours=24, keep_minimum=3
        ).removed

        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "db_full_0.dump",
            "db_full_1.dump",
            "db_full_2.dump",
        ]
        assert len(removed) == 2

    def test_keep_minimum_zero_deletes_everything_past_the_cutoff(self, tmp_path: Path):
        """The default is the pre-#339 behaviour, unchanged."""
        for index in range(3):
            _dump(tmp_path, f"db_full_{index}.dump", hours=48 + index)

        removed = cleanup_old_backups(tmp_path, retention_hours=24).removed

        assert len(removed) == 3
        assert list(tmp_path.iterdir()) == []

    def test_keep_minimum_exceeding_corpus_size_deletes_nothing(self, tmp_path: Path):
        for index in range(2):
            _dump(tmp_path, f"db_full_{index}.dump", hours=48 + index)

        removed = cleanup_old_backups(
            tmp_path, retention_hours=24, keep_minimum=10
        ).removed

        assert removed == ()
        assert len(list(tmp_path.iterdir())) == 2

    def test_exemption_is_by_mtime_not_by_filename(self, tmp_path: Path):
        """Timestamps in filenames lie; a re-sync rewrites mtime, not the name.

        ``zzz`` sorts last by name and is the newest by mtime, so a
        name-ordered implementation would delete exactly the file the
        floor exists to protect.
        """
        newest = _dump(tmp_path, "zzz_full.dump", hours=48)
        oldest = _dump(tmp_path, "aaa_full.dump", hours=200)

        cleanup_old_backups(tmp_path, retention_hours=24, keep_minimum=1)

        assert newest.exists()
        assert not oldest.exists()

    def test_directory_dumps_count_toward_the_floor(self, tmp_path: Path):
        """``-Fd`` trees occupy floor slots exactly like file dumps."""
        for index in range(3):
            tree = tmp_path / f"db_full_{index}.dump"
            tree.mkdir()
            (tree / "toc.dat").write_text("toc")
            _aged(tree, hours=48 + index)

        removed = cleanup_old_backups(
            tmp_path, retention_hours=24, keep_minimum=2
        ).removed

        assert len(removed) == 1
        assert (tmp_path / "db_full_0.dump").is_dir()
        assert (tmp_path / "db_full_1.dump").is_dir()
        assert not (tmp_path / "db_full_2.dump").exists()


class TestCleanupMatch:
    """The glob: one directory can hold more than one artifact class."""

    def test_match_restricts_the_glob_to_one_artifact_class(self, tmp_path: Path):
        """Full and slim dumps share a directory and expire on different clocks."""
        full = _dump(tmp_path, "db_full_20260101.dump", hours=48)
        slim = _dump(tmp_path, "db_slim_20260101.dump", hours=48)

        removed = cleanup_old_backups(
            tmp_path, retention_hours=24, match="*_full_*.dump"
        ).removed

        assert removed == (str(full),)
        assert not full.exists()
        assert slim.exists()

    def test_match_defaults_to_star_dump(self, tmp_path: Path):
        full = _dump(tmp_path, "db_full_20260101.dump", hours=48)
        slim = _dump(tmp_path, "db_slim_20260101.dump", hours=48)
        other = _dump(tmp_path, "notes.txt", hours=48)

        removed = cleanup_old_backups(tmp_path, retention_hours=24).removed

        assert sorted(removed) == sorted([str(full), str(slim)])
        assert other.exists()

    def test_containment_guard_applies_under_match(self, tmp_path: Path):
        """A symlink matching the glob but resolving outside is skipped.

        Re-pins the existing guard against the new code path: the guard
        lives past the exemption slice now, and its absence there is what
        a refactor would silently drop.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        target = _dump(outside, "victim_full.dump", hours=48)

        corpus = tmp_path / "corpus"
        corpus.mkdir()
        link = corpus / "db_full_20260101.dump"
        link.symlink_to(target)
        _aged(link, hours=48)

        removed = cleanup_old_backups(
            corpus, retention_hours=24, match="*_full_*.dump"
        ).removed

        assert removed == ()
        assert target.exists()
        assert link.is_symlink()


@pytest.mark.usefixtures("dumps_are_valid")
class TestCleanupOutcome:
    """The prune reports what it did, including why anything survived."""

    def test_outcome_reports_removed_kept_and_exempted(self, tmp_path: Path):
        """The three tuples partition the matched corpus."""
        fresh = _dump(tmp_path, "db_full_fresh.dump", hours=1)
        exempt = _dump(tmp_path, "db_full_exempt.dump", hours=48)
        doomed = _dump(tmp_path, "db_full_doomed.dump", hours=72)

        outcome = cleanup_old_backups(tmp_path, retention_hours=24, keep_minimum=2)

        assert outcome.removed == (str(doomed),)
        assert outcome.kept == (str(fresh),)
        assert outcome.exempted_by_minimum == (str(exempt),)

    def test_floor_was_load_bearing_when_every_survivor_is_past_the_cutoff(
        self, tmp_path: Path
    ):
        """The stalled-producer signal, knowable only here.

        Nothing survived on its own merits — the floor is the only
        reason the corpus is not empty. That is the state that preceded
        the outage, and it cannot be reconstructed from a list of
        deletions.
        """
        for index in range(4):
            _dump(tmp_path, f"db_full_{index}.dump", hours=48 + index)

        outcome = cleanup_old_backups(tmp_path, retention_hours=24, keep_minimum=2)

        assert outcome.floor_was_load_bearing is True

    def test_floor_not_load_bearing_when_a_survivor_is_within_retention(
        self, tmp_path: Path
    ):
        """A fresh dump means the producer is alive, floor or no floor."""
        _dump(tmp_path, "db_full_fresh.dump", hours=1)
        for index in range(3):
            _dump(tmp_path, f"db_full_{index}.dump", hours=48 + index)

        outcome = cleanup_old_backups(tmp_path, retention_hours=24, keep_minimum=2)

        assert outcome.exempted_by_minimum
        assert outcome.floor_was_load_bearing is False

    def test_floor_not_load_bearing_when_nothing_was_exempted(self, tmp_path: Path):
        """An empty corpus has no signal to report."""
        outcome = cleanup_old_backups(tmp_path, retention_hours=24, keep_minimum=3)

        assert outcome.removed == ()
        assert outcome.floor_was_load_bearing is False

    def test_dry_run_reports_what_it_would_remove_without_removing(
        self, tmp_path: Path
    ):
        """`--dry-run` reads the same selection the real prune acts on (#339).

        The alternative — the CLI deriving its own candidate list — would be
        a second authority for "what expires", which is the disease #337 and
        #336 are both instances of. It has to be the same code path or the
        preview is not a preview.
        """
        old_file = tmp_path / "old.dump"
        old_file.write_text("old")
        old_mtime = time.time() - 48 * 3600
        os.utime(old_file, (old_mtime, old_mtime))
        recent = tmp_path / "recent.dump"
        recent.write_text("recent")

        outcome = cleanup_old_backups(tmp_path, retention_hours=24, dry_run=True)

        assert outcome.removed == (str(old_file),)
        assert old_file.exists(), "dry_run deleted a file"
        assert outcome.kept == (str(recent),)

    def test_dry_run_still_honours_the_containment_guard(self, tmp_path: Path):
        """A preview must not promise to delete something the real run skips."""
        outside = tmp_path.parent / "outside.dump"
        outside.write_text("elsewhere")
        old_mtime = time.time() - 48 * 3600
        os.utime(outside, (old_mtime, old_mtime))
        backup_dir = tmp_path / "corpus"
        backup_dir.mkdir()
        (backup_dir / "link.dump").symlink_to(outside)

        outcome = cleanup_old_backups(backup_dir, retention_hours=24, dry_run=True)

        assert outcome.removed == ()
        assert outside.exists()

"""Tests for scaffold diff functionality."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fraisier.scaffold.diff import (
    FileDiff,
    _compare_files,
    _file_matches_filters,
    _sudo_copy,
    apply_scaffold_diffs,
)


def test_compute_scaffold_diff_matching_files():
    """Test compute_scaffold_diff with matching files returns status=match."""
    # Create a temporary directory for testing
    with tempfile.TemporaryDirectory() as temp_dir:
        scaffold_dir = Path(temp_dir)
        installed_dir = Path(temp_dir) / "installed"
        installed_dir.mkdir()

        # Create test files
        scaffold_file = scaffold_dir / "test.txt"
        scaffold_file.write_text("test content\n")

        installed_file = installed_dir / "test.txt"
        installed_file.write_text("test content\n")

        # Test the _compare_files function directly
        from fraisier.scaffold.diff import _compare_files

        result = _compare_files(scaffold_file, installed_file)

        assert result.status == "match"
        assert result.generated_path.endswith("test.txt")
        assert result.installed_path == installed_file
        assert result.diff_lines is None


def test_file_diff_dataclass():
    """Test FileDiff dataclass creation."""
    diff = FileDiff(
        generated_path="systemd/test.service",
        installed_path=Path("/etc/systemd/system/test.service"),
        status="differs",
        diff_lines=["--- old", "+++ new", "@@ -1 +1 @@", "-old", "+new"],
    )

    assert diff.generated_path == "systemd/test.service"
    assert diff.installed_path == Path("/etc/systemd/system/test.service")
    assert diff.status == "differs"
    assert diff.diff_lines == ["--- old", "+++ new", "@@ -1 +1 @@", "-old", "+new"]


def test_compare_files_permission_denied_on_exists():
    """_compare_files returns permission_denied when exists() raises PermissionError."""
    from fraisier.scaffold.diff import _compare_files

    with tempfile.TemporaryDirectory() as temp_dir:
        scaffold_file = Path(temp_dir) / "test.txt"
        scaffold_file.write_text("content\n")
        installed_path = Path("/etc/sudoers.d/myapp")

        perm_err = PermissionError("Permission denied")
        with patch.object(Path, "exists", side_effect=perm_err):
            result = _compare_files(scaffold_file, installed_path)

    assert result.status == "permission_denied"
    assert result.diff_lines is None


def test_compare_files_differs():
    """_compare_files returns differs when files have different content."""
    with tempfile.TemporaryDirectory() as temp_dir:
        scaffold_file = Path(temp_dir) / "test.txt"
        scaffold_file.write_text("new content\n")

        installed_file = Path(temp_dir) / "installed.txt"
        installed_file.write_text("old content\n")

        result = _compare_files(scaffold_file, installed_file)

        assert result.status == "differs"
        assert result.diff_lines is not None
        assert len(result.diff_lines) > 0


def test_compare_files_missing_installed():
    """_compare_files returns missing_installed when installed file doesn't exist."""
    with tempfile.TemporaryDirectory() as temp_dir:
        scaffold_file = Path(temp_dir) / "test.txt"
        scaffold_file.write_text("content\n")

        installed_file = Path(temp_dir) / "nonexistent.txt"

        result = _compare_files(scaffold_file, installed_file)

        assert result.status == "missing_installed"
        assert result.diff_lines is None


def test_compare_files_oserror():
    """_compare_files returns differs with error when read fails."""
    with tempfile.TemporaryDirectory() as temp_dir:
        scaffold_file = Path(temp_dir) / "test.txt"
        scaffold_file.write_text("content\n")

        installed_file = Path(temp_dir) / "installed.txt"
        installed_file.write_text("content\n")

        with patch.object(Path, "open", side_effect=OSError("Permission denied")):
            result = _compare_files(scaffold_file, installed_file)

        assert result.status == "differs"
        assert result.diff_lines is not None
        assert "Error reading files" in result.diff_lines[0]


def test_file_matches_filters_empty_paths():
    """_file_matches_filters accepts all paths when deploy set is empty."""
    result = _file_matches_filters("systemd/deploy.service", set())
    assert result is True


def test_file_matches_filters_systemd_deploy_match():
    """_file_matches_filters accepts deploy socket if in allowed list."""
    allowed = {"systemd/fraisier-prod.socket", "systemd/fraisier-prod@.service"}
    result = _file_matches_filters("systemd/fraisier-prod.socket", allowed)
    assert result is True


def test_file_matches_filters_non_deploy():
    """_file_matches_filters accepts non-deploy files regardless of deploy set."""
    allowed = {"systemd/fraisier-prod.socket"}
    result = _file_matches_filters("nginx/myapp.conf", allowed)
    assert result is True


def test_sudo_copy_success():
    """_sudo_copy returns (True, empty_string) on success."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        success, error = _sudo_copy(Path("/tmp/src"), Path("/tmp/dst"))

    assert success is True
    assert error == ""


def test_sudo_copy_failure():
    """_sudo_copy returns (False, stderr) on failure."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "Permission denied"
        success, error = _sudo_copy(Path("/tmp/src"), Path("/tmp/dst"))

    assert success is False
    assert error == "Permission denied"


def test_apply_scaffold_diffs_noop_when_nothing_to_apply():
    """apply_scaffold_diffs returns empty lists when all diffs are match status."""
    diffs = [
        FileDiff(
            generated_path="systemd/test.service",
            installed_path=Path("/etc/systemd/system/test.service"),
            status="match",
        )
    ]

    with patch("subprocess.run") as mock_run:
        applied, failed = apply_scaffold_diffs(MagicMock(), diffs)

    assert applied == []
    assert failed == []
    mock_run.assert_not_called()


def test_orphan_scan_survives_an_unreadable_install_directory():
    """`scaffold-diff` must not abort on a directory it cannot stat.

    `Path.exists()` swallows only ENOENT/ENOTDIR/EBADF/ELOOP — EACCES is
    re-raised. `/etc/sudoers.d` is mode 0750 root:root, so the orphan scan
    aborted the entire diff for any non-root caller, before it could
    report a single missing unit. Found while adding #339's retention
    units to the diff; the bug predates them and affects every artifact.
    """
    from fraisier.scaffold.diff import _compare_files

    denied = MagicMock(spec=Path)
    denied.exists.side_effect = PermissionError(13, "Permission denied")

    generated = MagicMock(spec=Path)
    generated.parent.parent = "root"
    generated.relative_to.return_value = "sudoers"

    result = _compare_files(generated, denied)

    assert result.status == "permission_denied"

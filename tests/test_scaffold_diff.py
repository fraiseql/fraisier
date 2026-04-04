"""Tests for scaffold diff functionality."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from fraisier.scaffold.diff import FileDiff


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

"""Tests for scaffold diff functionality."""

import tempfile
from pathlib import Path

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

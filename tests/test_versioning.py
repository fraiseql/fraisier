"""Tests for version management."""

import json

import pytest

from fraisier.versioning import (
    VersionInfo,
    bump_version,
    derive_database_version,
    has_version_changed,
    is_valid_semver,
    parse_semver,
    read_version,
    sync_pyproject_version,
    write_version,
)


class TestSemver:
    """Test semver validation and parsing."""

    @pytest.mark.parametrize(
        "version",
        ["0.0.0", "1.2.3", "10.20.30", "0.1.0"],
    )
    def test_is_valid_semver_valid(self, version):
        """Valid semver strings are accepted."""
        assert is_valid_semver(version) is True

    @pytest.mark.parametrize(
        "version",
        ["1.2", "v1.2.3", "1.2.3.4", "abc", "1.2.3-beta", ""],
    )
    def test_is_valid_semver_invalid(self, version):
        """Invalid semver strings are rejected."""
        assert is_valid_semver(version) is False

    def test_parse_semver_valid(self):
        """parse_semver returns (major, minor, patch) tuple."""
        assert parse_semver("1.2.3") == (1, 2, 3)
        assert parse_semver("0.0.0") == (0, 0, 0)

    def test_parse_semver_invalid(self):
        """parse_semver raises ValueError for invalid input."""
        with pytest.raises(ValueError, match="Invalid semver"):
            parse_semver("not.a.version")


class TestVersionInfo:
    """Test VersionInfo dataclass."""

    def test_version_info_to_dict(self):
        """to_dict serializes all fields."""
        info = VersionInfo(version="1.0.0", commit="abc123", branch="main")
        d = info.to_dict()
        assert d["version"] == "1.0.0"
        assert d["commit"] == "abc123"
        assert d["branch"] == "main"
        assert "timestamp" in d

    def test_version_info_from_dict(self):
        """from_dict deserializes known keys and ignores unknown."""
        data = {
            "version": "2.0.0",
            "commit": "def456",
            "unknown_key": "ignored",
        }
        info = VersionInfo.from_dict(data)
        assert info.version == "2.0.0"
        assert info.commit == "def456"


class TestVersionIO:
    """Test write_version and read_version."""

    def test_write_read_version(self, tmp_path):
        """Round-trip write then read preserves data."""
        path = tmp_path / "version.json"
        info = VersionInfo(version="1.2.3", branch="main")
        write_version(info, path)

        loaded = read_version(path)
        assert loaded is not None
        assert loaded.version == "1.2.3"
        assert loaded.branch == "main"

    def test_read_version_missing(self, tmp_path):
        """read_version returns None for missing file."""
        path = tmp_path / "nonexistent.json"
        assert read_version(path) is None


class TestBumpVersion:
    """Test bump_version function."""

    def _write_version_file(self, tmp_path, version="1.2.3"):
        path = tmp_path / "version.json"
        info = VersionInfo(version=version)
        write_version(info, path)
        return path

    def test_bump_major(self, tmp_path):
        """Major bump increments major and resets minor/patch."""
        path = self._write_version_file(tmp_path, "1.2.3")
        result = bump_version(path, "major")
        assert result.version == "2.0.0"

    def test_bump_minor(self, tmp_path):
        """Minor bump increments minor and resets patch."""
        path = self._write_version_file(tmp_path, "1.2.3")
        result = bump_version(path, "minor")
        assert result.version == "1.3.0"

    def test_bump_patch(self, tmp_path):
        """Patch bump increments patch only."""
        path = self._write_version_file(tmp_path, "1.2.3")
        result = bump_version(path, "patch")
        assert result.version == "1.2.4"

    def test_bump_creates_backup(self, tmp_path):
        """bump_version creates a .bak file."""
        path = self._write_version_file(tmp_path, "1.0.0")
        bump_version(path, "patch")
        backup = path.with_suffix(".json.bak")
        assert backup.exists()
        backup_data = json.loads(backup.read_text())
        assert backup_data["version"] == "1.0.0"

    def test_bump_invalid_part(self, tmp_path):
        """bump_version raises ValueError for invalid part."""
        path = self._write_version_file(tmp_path)
        with pytest.raises(ValueError, match="Invalid bump part"):
            bump_version(path, "hotfix")

    def test_bump_missing_file(self, tmp_path):
        """bump_version raises FileNotFoundError for missing file."""
        path = tmp_path / "missing.json"
        with pytest.raises(FileNotFoundError):
            bump_version(path, "patch")


class TestSyncPyprojectVersion:
    """Test sync_pyproject_version."""

    def test_sync_pyproject_version(self, tmp_path):
        """Updates the version field in pyproject.toml."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "myapp"\nversion = "0.1.0"\n')
        sync_pyproject_version("2.0.0", pyproject)
        content = pyproject.read_text()
        assert 'version = "2.0.0"' in content


class TestDeriveAndCompare:
    """Test derive_database_version and has_version_changed."""

    def test_derive_database_version(self):
        """Derived version follows YYYY.MM.DD.NNN format."""
        result = derive_database_version(sequence=42)
        parts = result.split(".")
        assert len(parts) == 4
        assert parts[3] == "042"
        # Year should be 4 digits
        assert len(parts[0]) == 4

    def test_has_version_changed(self):
        """has_version_changed detects changes and handles None old version."""
        assert has_version_changed(None, "1.0.0") is True
        assert has_version_changed("1.0.0", "1.0.0") is False
        assert has_version_changed("1.0.0", "2.0.0") is True

"""Tests for atomic version bumping across version.json and pyproject.toml."""

import contextlib
import json

from fraisier.versioning import VersionInfo, read_version, write_version


def _setup_version_files(tmp_path, version="1.0.0"):
    """Create version.json and pyproject.toml in tmp_path."""
    version_path = tmp_path / "version.json"
    pyproject_path = tmp_path / "pyproject.toml"

    info = VersionInfo(
        version=version,
        commit="abc123",
        branch="main",
        timestamp="2026-03-23T12:00:00Z",
        schema_hash="sha256:deadbeef",
    )
    write_version(info, version_path)

    pyproject_path.write_text(f'[project]\nname = "myapp"\nversion = "{version}"\n')
    return version_path, pyproject_path


class TestAtomicBump:
    """Test bump_version updates both files atomically."""

    def test_patch_bump_updates_both_files(self, tmp_path):
        """Test patch bump updates version.json and pyproject.toml."""
        from fraisier.versioning import bump_version

        vp, pp = _setup_version_files(tmp_path, "1.2.3")

        result = bump_version(vp, "patch", pyproject_path=pp)
        assert result.version == "1.2.4"

        # version.json updated
        info = read_version(vp)
        assert info.version == "1.2.4"

        # pyproject.toml updated
        content = pp.read_text()
        assert 'version = "1.2.4"' in content

    def test_minor_bump_updates_both_files(self, tmp_path):
        """Test minor bump updates both files."""
        from fraisier.versioning import bump_version

        vp, pp = _setup_version_files(tmp_path, "1.2.3")

        result = bump_version(vp, "minor", pyproject_path=pp)
        assert result.version == "1.3.0"

        content = pp.read_text()
        assert 'version = "1.3.0"' in content

    def test_major_bump_updates_both_files(self, tmp_path):
        """Test major bump updates both files."""
        from fraisier.versioning import bump_version

        vp, pp = _setup_version_files(tmp_path, "1.2.3")

        result = bump_version(vp, "major", pyproject_path=pp)
        assert result.version == "2.0.0"

        content = pp.read_text()
        assert 'version = "2.0.0"' in content

    def test_atomic_rollback_on_pyproject_failure(self, tmp_path):
        """Test neither file is updated if pyproject write fails."""
        from unittest.mock import patch

        from fraisier.versioning import bump_version

        vp, pp = _setup_version_files(tmp_path, "1.0.0")

        # Make pyproject_path read-only to cause write failure
        with (
            patch(
                "fraisier.versioning.sync_pyproject_version",
                side_effect=OSError("disk full"),
            ),
            contextlib.suppress(OSError),
        ):
            bump_version(vp, "patch", pyproject_path=pp)

        # version.json should NOT be updated
        info = read_version(vp)
        assert info.version == "1.0.0"

        # pyproject.toml should NOT be updated
        content = pp.read_text()
        assert 'version = "1.0.0"' in content

    def test_bump_without_pyproject_still_works(self, tmp_path):
        """Test bump_version works without pyproject_path (backward compat)."""
        from fraisier.versioning import bump_version

        vp, _pp = _setup_version_files(tmp_path, "1.0.0")

        result = bump_version(vp, "patch")
        assert result.version == "1.0.1"


class TestVersionJsonFields:
    """Test version.json includes all required metadata fields."""

    def test_version_json_has_required_fields(self, tmp_path):
        """Test version.json contains all required fields."""
        from fraisier.versioning import bump_version

        vp, pp = _setup_version_files(tmp_path, "1.0.0")
        bump_version(vp, "patch", pyproject_path=pp)

        data = json.loads(vp.read_text())
        assert "version" in data
        assert "commit" in data
        assert "branch" in data
        assert "timestamp" in data
        assert "schema_hash" in data

    def test_version_json_preserves_metadata(self, tmp_path):
        """Test bump preserves commit, branch, timestamp, schema_hash."""
        from fraisier.versioning import bump_version

        vp, pp = _setup_version_files(tmp_path, "1.0.0")
        result = bump_version(vp, "patch", pyproject_path=pp)

        assert result.commit == "abc123"
        assert result.branch == "main"
        assert result.timestamp == "2026-03-23T12:00:00Z"
        assert result.schema_hash == "sha256:deadbeef"

    def test_patch_resets_nothing(self, tmp_path):
        """Test patch bump only increments patch."""
        from fraisier.versioning import bump_version

        vp, pp = _setup_version_files(tmp_path, "2.5.9")
        result = bump_version(vp, "patch", pyproject_path=pp)
        assert result.version == "2.5.10"

    def test_minor_resets_patch(self, tmp_path):
        """Test minor bump resets patch to 0."""
        from fraisier.versioning import bump_version

        vp, pp = _setup_version_files(tmp_path, "2.5.9")
        result = bump_version(vp, "minor", pyproject_path=pp)
        assert result.version == "2.6.0"

    def test_major_resets_minor_and_patch(self, tmp_path):
        """Test major bump resets minor and patch to 0."""
        from fraisier.versioning import bump_version

        vp, pp = _setup_version_files(tmp_path, "2.5.9")
        result = bump_version(vp, "major", pyproject_path=pp)
        assert result.version == "3.0.0"

"""Tests for atomic version bumping via pyproject.toml."""

import contextlib

import pytest

from fraisier.versioning import VersionInfo, VersionSyncConfig, VersionSyncTarget


def _setup_pyproject(tmp_path, version="1.2.3"):
    """Create a pyproject.toml in tmp_path with the given version."""
    p = tmp_path / "pyproject.toml"
    p.write_text(f'[project]\nname = "myapp"\nversion = "{version}"\n')
    return p


class TestAtomicBump:
    """bump_version updates pyproject.toml atomically."""

    def test_patch_bump(self, tmp_path):
        """Patch bump increments patch component."""
        from fraisier.versioning import bump_version

        pp = _setup_pyproject(tmp_path, "1.2.3")
        result = bump_version(pp, "patch")
        assert result.version == "1.2.4"
        assert 'version = "1.2.4"' in pp.read_text()

    def test_minor_bump(self, tmp_path):
        """Minor bump increments minor, resets patch."""
        from fraisier.versioning import bump_version

        pp = _setup_pyproject(tmp_path, "1.2.3")
        result = bump_version(pp, "minor")
        assert result.version == "1.3.0"
        assert 'version = "1.3.0"' in pp.read_text()

    def test_major_bump(self, tmp_path):
        """Major bump increments major, resets minor and patch."""
        from fraisier.versioning import bump_version

        pp = _setup_pyproject(tmp_path, "1.2.3")
        result = bump_version(pp, "major")
        assert result.version == "2.0.0"
        assert 'version = "2.0.0"' in pp.read_text()

    def test_invalid_part_raises(self, tmp_path):
        """Invalid bump part raises ValueError."""
        from fraisier.versioning import bump_version

        pp = _setup_pyproject(tmp_path, "1.0.0")
        with pytest.raises(ValueError, match="Invalid bump part"):
            bump_version(pp, "bad")

    def test_missing_file_raises(self, tmp_path):
        """FileNotFoundError if pyproject.toml does not exist."""
        from fraisier.versioning import bump_version

        with pytest.raises(FileNotFoundError):
            bump_version(tmp_path / "pyproject.toml", "patch")

    def test_atomic_rollback_on_write_failure(self, tmp_path):
        """pyproject.toml is untouched if a sync target write fails."""
        from fraisier.versioning import (
            VersionSyncConfig,
            VersionSyncTarget,
            bump_version,
        )

        pp = _setup_pyproject(tmp_path, "1.0.0")
        config = VersionSyncConfig(
            targets=[
                VersionSyncTarget(
                    path=tmp_path / "missing.txt",
                    regex=r'version\s*=\s*"([^"]+)"',
                )
            ]
        )
        with contextlib.suppress(FileNotFoundError):
            bump_version(pp, "patch", sync_config=config)

        assert 'version = "1.0.0"' in pp.read_text()

    def test_bump_with_sync_config(self, tmp_path):
        """bump_version syncs to additional targets via sync_config."""
        from fraisier.versioning import bump_version

        pp = _setup_pyproject(tmp_path, "0.9.0")
        init_file = tmp_path / "__init__.py"
        init_file.write_text('__version__ = "0.9.0"\n')

        sync_config = VersionSyncConfig(
            targets=[
                VersionSyncTarget(
                    path=init_file, regex=r'^(__version__\s*=\s*")([^"]+)(")'
                )
            ]
        )
        result = bump_version(pp, "minor", sync_config=sync_config)
        assert result.version == "0.10.0"
        assert '__version__ = "0.10.0"' in init_file.read_text()

    def test_returns_version_info(self, tmp_path):
        """bump_version returns a VersionInfo with the new version."""
        from fraisier.versioning import bump_version

        pp = _setup_pyproject(tmp_path, "3.1.4")
        result = bump_version(pp, "patch")
        assert isinstance(result, VersionInfo)
        assert result.version == "3.1.5"


class TestRefreshUvLock:
    """refresh_uv_lock keeps uv.lock's self-version in step after a bump (#328)."""

    def test_no_lockfile_is_a_noop(self, tmp_path):
        """Without uv.lock there is nothing to refresh and no warning."""
        from unittest.mock import patch

        from fraisier.versioning import refresh_uv_lock

        with patch("subprocess.run") as mock_run:
            assert refresh_uv_lock(tmp_path) is None
        mock_run.assert_not_called()

    def test_runs_uv_lock_in_project_dir(self, tmp_path):
        """With uv.lock present, `uv lock` runs in the project directory."""
        from unittest.mock import MagicMock, patch

        from fraisier.versioning import refresh_uv_lock

        (tmp_path / "uv.lock").write_text("")
        with (
            patch("shutil.which", return_value="/usr/bin/uv"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            assert refresh_uv_lock(tmp_path) is None
        args, kwargs = mock_run.call_args
        assert args[0] == ["/usr/bin/uv", "lock"]
        assert kwargs["cwd"] == tmp_path

    def test_missing_uv_warns_instead_of_raising(self, tmp_path):
        """uv absent from PATH degrades to a warning, not a crash."""
        from unittest.mock import patch

        from fraisier.versioning import refresh_uv_lock

        (tmp_path / "uv.lock").write_text("")
        with patch("shutil.which", return_value=None):
            warning = refresh_uv_lock(tmp_path)
        assert warning is not None
        assert "not on PATH" in warning

    def test_failed_lock_warns_with_stderr_tail(self, tmp_path):
        """A failing `uv lock` returns a warning carrying the error tail."""
        from unittest.mock import MagicMock, patch

        from fraisier.versioning import refresh_uv_lock

        (tmp_path / "uv.lock").write_text("")
        with (
            patch("shutil.which", return_value="/usr/bin/uv"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=1, stderr="boom\nresolver exploded", stdout=""
            )
            warning = refresh_uv_lock(tmp_path)
        assert warning is not None
        assert "resolver exploded" in warning

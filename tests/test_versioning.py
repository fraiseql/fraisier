"""Tests for version management."""

import json
from pathlib import Path

import pytest

from fraisier.strategies import (
    AlembicMigrateStrategy,
    ConfitureMigrateStrategy,
    DjangoMigrateStrategy,
    PeeweeMigrateStrategy,
    ValidationResult,
)

from fraisier.versioning import (
    VersionInfo,
    VersionSyncConfig,
    VersionSyncTarget,
    bump_version,
    derive_database_version,
    has_version_changed,
    is_valid_semver,
    parse_semver,
    read_version,
    sync_version_to_targets,
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

    def test_bump_with_sync_config(self, tmp_path):
        """bump_version syncs to configured targets."""
        # Set up version.json
        path = self._write_version_file(tmp_path, "1.0.0")

        # Set up target file
        target_file = tmp_path / "version.py"
        target_file.write_text('__version__ = "1.0.0"\n')

        # Configure sync
        config = VersionSyncConfig(
            targets=[
                VersionSyncTarget(
                    path=target_file, regex=r'^(__version__\s*=\s*")([^"]+)(")'
                )
            ]
        )

        result = bump_version(path, "minor", sync_config=config)
        assert result.version == "1.1.0"

        # Check target was updated
        content = target_file.read_text()
        assert '__version__ = "1.1.0"' in content

    def test_bump_sync_config_rollback_on_failure(self, tmp_path):
        """bump_version rolls back version.json if target sync fails."""
        # Set up version.json
        path = self._write_version_file(tmp_path, "1.0.0")

        # Set up config with non-existent target
        config = VersionSyncConfig(
            targets=[
                VersionSyncTarget(
                    path=tmp_path / "missing.txt", regex=r'version\s*=\s*"([^"]+)"'
                )
            ]
        )

        with pytest.raises(FileNotFoundError):
            bump_version(path, "patch", sync_config=config)

        # version.json should be unchanged due to rollback
        info = read_version(path)
        assert info.version == "1.0.0"


class TestDjangoMigrateStrategy:
    """Test Django migration strategy."""

    def test_django_strategy_creation(self):
        """Can create Django migration strategy."""
        strategy = DjangoMigrateStrategy("myapp.settings", "myapp")
        assert strategy.framework_name == "django"
        assert strategy.settings_module == "myapp.settings"
        assert strategy.app_label == "myapp"

    def test_django_strategy_creation_no_app_label(self):
        """Can create Django strategy without app label."""
        strategy = DjangoMigrateStrategy("myapp.settings")
        assert strategy.app_label is None


class TestAlembicMigrateStrategy:
    """Test Alembic migration strategy."""

    def test_alembic_strategy_creation(self):
        """Can create Alembic migration strategy."""
        strategy = AlembicMigrateStrategy(
            script_location="db/migrations",
            ini_path="alembic.ini",
            environment="production",
        )
        assert strategy.framework_name == "alembic"
        assert strategy.script_location == Path("db/migrations")
        assert strategy.ini_path == Path("alembic.ini")
        assert strategy.environment == "production"


class TestPeeweeMigrateStrategy:
    """Test Peewee migration strategy."""

    def test_peewee_strategy_creation(self):
        """Can create Peewee migration strategy."""
        strategy = PeeweeMigrateStrategy(
            models_module="myapp.models", migrations_dir="migrations"
        )
        assert strategy.framework_name == "peewee"
        assert strategy.models_module == "myapp.models"
        assert strategy.migrations_dir == Path("migrations")


class TestConfitureMigrateStrategy:
    """Test Confiture migration strategy."""

    def test_confiture_strategy_creation(self):
        """Can create Confiture migration strategy."""
        strategy = ConfitureMigrateStrategy("confiture.yaml")
        assert strategy.framework_name == "confiture"
        assert strategy.config_file == Path("confiture.yaml")


class TestVersionSyncConfig:
    """Test VersionSyncConfig functionality."""

    def test_from_dict_creates_targets(self):
        """Creates sync config from dict with sync_to list."""
        data = {
            "sync_to": [
                {"path": "pyproject.toml", "regex": r'version\s*=\s*"([^"]+)"'},
                {"path": "src/__init__.py", "regex": r'__version__\s*=\s*"([^"]+)"'},
            ]
        }
        config = VersionSyncConfig.from_dict(data)
        assert len(config.targets) == 2
        assert str(config.targets[0].path) == "pyproject.toml"
        assert config.targets[0].regex == r'version\s*=\s*"([^"]+)"'

    def test_auto_discover_pyproject(self, tmp_path):
        """Auto-discovers pyproject.toml."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "myapp"\nversion = "0.1.0"\n')

        config = VersionSyncConfig.auto_discover(tmp_path)
        assert len(config.targets) == 1
        assert config.targets[0].path == pyproject

    def test_auto_discover_init_files(self, tmp_path):
        """Auto-discovers __init__.py files with __version__."""
        init_file = tmp_path / "src" / "mypackage" / "__init__.py"
        init_file.parent.mkdir(parents=True)
        init_file.write_text('__version__ = "1.0.0"\n')

        config = VersionSyncConfig.auto_discover(tmp_path)
        assert len(config.targets) == 1
        assert config.targets[0].path == init_file

    def test_auto_discover_skips_init_without_version(self, tmp_path):
        """Skips __init__.py files without __version__."""
        init_file = tmp_path / "src" / "mypackage" / "__init__.py"
        init_file.parent.mkdir(parents=True)
        init_file.write_text('name = "mypackage"\n')

        config = VersionSyncConfig.auto_discover(tmp_path)
        assert len(config.targets) == 0


class TestSyncVersionToTargets:
    """Test sync_version_to_targets functionality."""

    def test_sync_single_target(self, tmp_path):
        """Updates version in a single target file."""
        target_file = tmp_path / "version.txt"
        target_file.write_text('VERSION = "1.0.0"')

        config = VersionSyncConfig(
            targets=[
                VersionSyncTarget(
                    path=target_file, regex=r'^(VERSION\s*=\s*")([^"]+)(")'
                )
            ]
        )

        sync_version_to_targets("2.0.0", config)
        content = target_file.read_text()
        assert 'VERSION = "2.0.0"' in content

    def test_sync_multiple_targets(self, tmp_path):
        """Updates version in multiple target files atomically."""
        file1 = tmp_path / "file1.txt"
        file1.write_text('version = "1.0.0"')
        file2 = tmp_path / "file2.txt"
        file2.write_text('__version__ = "1.0.0"')

        config = VersionSyncConfig(
            targets=[
                VersionSyncTarget(path=file1, regex=r'^(version\s*=\s*")([^"]+)(")'),
                VersionSyncTarget(
                    path=file2, regex=r'^(__version__\s*=\s*")([^"]+)(")'
                ),
            ]
        )

        sync_version_to_targets("2.0.0", config)
        assert 'version = "2.0.0"' in file1.read_text()
        assert '__version__ = "2.0.0"' in file2.read_text()

    def test_sync_rollback_on_failure(self, tmp_path):
        """Rolls back all changes if any target fails."""
        file1 = tmp_path / "file1.txt"
        file1.write_text('version = "1.0.0"')
        file2 = tmp_path / "file2.txt"  # This file won't exist

        config = VersionSyncConfig(
            targets=[
                VersionSyncTarget(path=file1, regex=r'version\s*=\s*"([^"]+)"'),
                VersionSyncTarget(path=file2, regex=r'__version__\s*=\s*"([^"]+)"'),
            ]
        )

        with pytest.raises(FileNotFoundError):
            sync_version_to_targets("2.0.0", config)

        # file1 should be unchanged due to rollback
        assert 'version = "1.0.0"' in file1.read_text()


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

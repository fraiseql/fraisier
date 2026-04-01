# Phase 1, Cycle 1-4: ConfigWatcher tests (RED phase)
"""Tests for config synchronization and change detection."""

import pytest

from fraisier.config_watcher import ConfigWatcher


class TestConfigWatcherHashComputation:
    """Tests for ConfigWatcher.compute_hash()."""

    def test_compute_hash_returns_consistent_sha256(self, tmp_path):
        """compute_hash() returns consistent SHA256 for same file."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("test config")

        watcher = ConfigWatcher(tmp_path)
        hash1 = watcher.compute_hash()
        hash2 = watcher.compute_hash()

        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256 hex is 64 chars

    def test_compute_hash_changes_on_file_modification(self, tmp_path):
        """compute_hash() returns different hash after file modification."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("config v1")

        watcher = ConfigWatcher(tmp_path)
        hash1 = watcher.compute_hash()

        config_file.write_text("config v2")
        hash2 = watcher.compute_hash()

        assert hash1 != hash2

    def test_compute_hash_raises_when_file_missing(self, tmp_path):
        """compute_hash() raises FileNotFoundError if config doesn't exist."""
        # Don't create fraises.yaml
        watcher = ConfigWatcher(tmp_path)

        with pytest.raises(FileNotFoundError):
            watcher.compute_hash()

    def test_compute_hash_handles_large_files(self, tmp_path):
        """compute_hash() handles large files with chunked reading."""
        config_file = tmp_path / "fraises.yaml"
        # Create file larger than chunk size (8KB)
        large_content = "x" * (10 * 1024)
        config_file.write_text(large_content)

        watcher = ConfigWatcher(tmp_path)
        hash_value = watcher.compute_hash()

        # Verify hash is deterministic
        assert hash_value == watcher.compute_hash()
        assert len(hash_value) == 64


class TestConfigWatcherPreviousHash:
    """Tests for ConfigWatcher.get_previous_hash()."""

    def test_get_previous_hash_returns_none_when_not_exists(self, tmp_path):
        """get_previous_hash() returns None if .config_hash doesn't exist."""
        watcher = ConfigWatcher(tmp_path)
        assert watcher.get_previous_hash() is None

    def test_get_previous_hash_returns_stored_hash(self, tmp_path):
        """get_previous_hash() reads and returns stored hash."""
        hash_file = tmp_path / ".config_hash"
        stored_hash = "abcd1234" * 8  # 64 chars
        hash_file.write_text(stored_hash)

        watcher = ConfigWatcher(tmp_path)
        assert watcher.get_previous_hash() == stored_hash

    def test_get_previous_hash_strips_whitespace(self, tmp_path):
        """get_previous_hash() strips trailing whitespace."""
        hash_file = tmp_path / ".config_hash"
        stored_hash = "abcd1234" * 8
        hash_file.write_text(f"{stored_hash}\n")

        watcher = ConfigWatcher(tmp_path)
        assert watcher.get_previous_hash() == stored_hash

    def test_get_previous_hash_handles_read_error(self, tmp_path):
        """get_previous_hash() returns None on read error."""
        # Create unreadable file (set up scenario)
        hash_file = tmp_path / ".config_hash"
        hash_file.write_text("test")
        hash_file.chmod(0o000)

        watcher = ConfigWatcher(tmp_path)
        result = watcher.get_previous_hash()

        # Should return None instead of raising
        assert result is None

        # Cleanup
        hash_file.chmod(0o644)


class TestConfigWatcherChangeDetection:
    """Tests for ConfigWatcher.has_changed()."""

    def test_has_changed_returns_true_first_run(self, tmp_path):
        """has_changed() returns True when no previous hash (first run)."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("config v1")

        watcher = ConfigWatcher(tmp_path)
        assert watcher.has_changed() is True

    def test_has_changed_returns_false_when_unchanged(self, tmp_path):
        """has_changed() returns False when config unchanged."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("config v1")

        watcher = ConfigWatcher(tmp_path)
        watcher.save_hash()

        # Same config = no change
        assert watcher.has_changed() is False

    def test_has_changed_returns_true_when_modified(self, tmp_path):
        """has_changed() returns True when config modified."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("config v1")

        watcher = ConfigWatcher(tmp_path)
        watcher.save_hash()

        # Modify config
        config_file.write_text("config v2")

        assert watcher.has_changed() is True

    def test_has_changed_returns_true_when_config_missing(self, tmp_path):
        """has_changed() returns True if config file doesn't exist."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("config v1")

        watcher = ConfigWatcher(tmp_path)
        watcher.save_hash()

        # Delete config
        config_file.unlink()

        assert watcher.has_changed() is True


class TestConfigWatcherSaveHash:
    """Tests for ConfigWatcher.save_hash()."""

    def test_save_hash_writes_to_file(self, tmp_path):
        """save_hash() persists current hash to .config_hash."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("config v1")

        watcher = ConfigWatcher(tmp_path)
        watcher.save_hash()

        hash_file = tmp_path / ".config_hash"
        assert hash_file.exists()
        assert hash_file.read_text() == watcher.compute_hash()

    def test_save_hash_overwrites_previous(self, tmp_path):
        """save_hash() overwrites previous hash."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("config v1")

        watcher = ConfigWatcher(tmp_path)
        hash1 = watcher.compute_hash()
        watcher.save_hash()

        # Change config
        config_file.write_text("config v2")
        hash2 = watcher.compute_hash()
        watcher.save_hash()

        assert hash1 != hash2
        assert tmp_path.joinpath(".config_hash").read_text() == hash2

    def test_save_hash_raises_on_write_error(self, tmp_path):
        """save_hash() raises OSError if unable to write."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("config v1")

        # Make directory read-only to cause write error
        tmp_path.chmod(0o555)

        watcher = ConfigWatcher(tmp_path)
        with pytest.raises(OSError):
            watcher.save_hash()

        # Cleanup
        tmp_path.chmod(0o755)

    def test_save_hash_creates_file_if_not_exists(self, tmp_path):
        """save_hash() creates .config_hash if it doesn't exist."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("config v1")

        hash_file = tmp_path / ".config_hash"
        assert not hash_file.exists()

        watcher = ConfigWatcher(tmp_path)
        watcher.save_hash()

        assert hash_file.exists()


# Phase 2: Deployer Base Class Methods tests (RED phase)
class TestDeployerSyncFraisesYaml:
    """Tests for BaseDeployer._sync_fraises_yaml()."""

    def test_sync_fraises_yaml_method_exists(self, tmp_path):
        """_sync_fraises_yaml() method exists on deployer."""
        from fraisier.deployers.base import BaseDeployer

        assert hasattr(BaseDeployer, "_sync_fraises_yaml")

    def test_sync_fraises_yaml_is_callable(self, tmp_path):
        """_sync_fraises_yaml() is callable."""
        from fraisier.deployers.base import BaseDeployer

        assert callable(getattr(BaseDeployer, "_sync_fraises_yaml", None))


class TestDeployerDetectConfigChanges:
    """Tests for BaseDeployer._detect_config_changes()."""

    def test_detect_config_changes_method_exists(self, tmp_path):
        """_detect_config_changes() method exists on deployer."""
        from fraisier.deployers.base import BaseDeployer

        assert hasattr(BaseDeployer, "_detect_config_changes")

    def test_detect_config_changes_is_callable(self, tmp_path):
        """_detect_config_changes() is callable."""
        from fraisier.deployers.base import BaseDeployer

        assert callable(getattr(BaseDeployer, "_detect_config_changes", None))


class TestDeployerRegenerateScaffold:
    """Tests for BaseDeployer._regenerate_scaffold()."""

    def test_regenerate_scaffold_method_exists(self, tmp_path):
        """_regenerate_scaffold() method exists on deployer."""
        from fraisier.deployers.base import BaseDeployer

        assert hasattr(BaseDeployer, "_regenerate_scaffold")

    def test_regenerate_scaffold_is_callable(self, tmp_path):
        """_regenerate_scaffold() is callable."""
        from fraisier.deployers.base import BaseDeployer

        assert callable(getattr(BaseDeployer, "_regenerate_scaffold", None))


class TestDeployerInstallScaffold:
    """Tests for BaseDeployer._install_scaffold()."""

    def test_install_scaffold_method_exists(self, tmp_path):
        """_install_scaffold() method exists on deployer."""
        from fraisier.deployers.base import BaseDeployer

        assert hasattr(BaseDeployer, "_install_scaffold")

    def test_install_scaffold_is_callable(self, tmp_path):
        """_install_scaffold() is callable."""
        from fraisier.deployers.base import BaseDeployer

        assert callable(getattr(BaseDeployer, "_install_scaffold", None))

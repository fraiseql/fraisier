"""Integration tests for config synchronization during deployment."""



from fraisier.config_watcher import ConfigWatcher
from fraisier.deployers.api import APIDeployer


class TestConfigSyncIntegration:
    """Integration tests for config sync during deployment."""

    def test_config_watcher_full_lifecycle(self, tmp_path):
        """Test full lifecycle: detect -> save -> detect unchanged."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("config version 1")

        # Initial run: no previous hash
        watcher = ConfigWatcher(tmp_path)
        assert watcher.has_changed() is True

        # Save hash
        watcher.save_hash()
        hash_file = tmp_path / ".config_hash"
        assert hash_file.exists()
        stored_hash = hash_file.read_text()

        # Same config: no change
        watcher2 = ConfigWatcher(tmp_path)
        assert watcher2.has_changed() is False

        # Modify config
        config_file.write_text("config version 2")
        assert watcher2.has_changed() is True

        # Save new hash
        watcher2.save_hash()
        new_hash = hash_file.read_text()
        assert new_hash != stored_hash

    def test_multiple_config_changes_detected(self, tmp_path):
        """Test detection of multiple sequential config changes."""
        config_file = tmp_path / "fraises.yaml"
        hashes = []

        for i in range(5):
            config_file.write_text(f"config version {i}")
            watcher = ConfigWatcher(tmp_path)

            if i == 0:
                assert watcher.has_changed() is True
            else:
                # Should detect change from previous version
                assert watcher.has_changed() is True

            watcher.save_hash()
            hash_file = tmp_path / ".config_hash"
            hashes.append(hash_file.read_text())

        # All hashes should be different
        assert len(set(hashes)) == len(hashes)

    def test_config_watcher_with_yaml_like_content(self, tmp_path):
        """Test ConfigWatcher with realistic YAML-like content."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """fraises:
  api:
    environment: production
    database_url: postgresql://...
    systemd_service: api
"""
        )

        watcher = ConfigWatcher(tmp_path)
        hash1 = watcher.compute_hash()

        # Whitespace change should be detected
        config_file.write_text(
            """fraises:
  api:
    environment: production
    database_url: postgresql://...
    systemd_service: api

"""
        )

        hash2 = watcher.compute_hash()
        assert hash1 != hash2

    def test_config_sync_with_deployer(self, tmp_path):
        """Test config sync methods on APIDeployer."""
        # Setup source and destination
        app_path = tmp_path / "app"
        app_path.mkdir()
        source_file = app_path / "fraises.yaml"
        source_file.write_text("test config")

        opt_path = tmp_path / "opt"
        opt_path.mkdir()
        dest_file = opt_path / "fraises.yaml"

        config = {
            "fraise_name": "test_api",
            "environment": "production",
            "git_repo": str(app_path),
        }

        deployer = APIDeployer(config)

        # Test sync
        deployer._sync_fraises_yaml(source_path=source_file, dest_path=dest_file)
        assert dest_file.exists()
        assert dest_file.read_text() == "test config"

    def test_config_detection_integration(self, tmp_path):
        """Test config detection with real file operations."""
        app_path = tmp_path / "app"
        app_path.mkdir()
        source_file = app_path / "fraises.yaml"
        source_file.write_text("config v1")

        opt_path = tmp_path / "opt"
        opt_path.mkdir()
        dest_file = opt_path / "fraises.yaml"

        # First deployment
        watcher = ConfigWatcher(opt_path)

        # Simulate sync
        dest_file.write_text("config v1")
        assert watcher.has_changed() is True
        watcher.save_hash()
        assert watcher.has_changed() is False

        # Config update
        dest_file.write_text("config v2")
        assert watcher.has_changed() is True

    def test_config_change_detection_sequence(self, tmp_path):
        """Test sequence of deploys with config changes."""
        opt_path = tmp_path / "opt"
        opt_path.mkdir()
        config_file = opt_path / "fraises.yaml"

        # Deploy 1: Initial
        config_file.write_text("v1")
        watcher1 = ConfigWatcher(opt_path)
        assert watcher1.has_changed() is True
        watcher1.save_hash()

        # Deploy 2: Same config
        watcher2 = ConfigWatcher(opt_path)
        assert watcher2.has_changed() is False

        # Deploy 3: Config changed
        config_file.write_text("v2")
        watcher3 = ConfigWatcher(opt_path)
        assert watcher3.has_changed() is True
        watcher3.save_hash()

        # Deploy 4: Same as deploy 3
        watcher4 = ConfigWatcher(opt_path)
        assert watcher4.has_changed() is False

        # Deploy 5: Config changed again
        config_file.write_text("v3")
        watcher5 = ConfigWatcher(opt_path)
        assert watcher5.has_changed() is True

    def test_concurrent_hash_file_access(self, tmp_path):
        """Test concurrent access to hash file (multiple watchers)."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("config")

        # Multiple watcher instances accessing the same hash file
        watcher1 = ConfigWatcher(tmp_path)
        watcher1.save_hash()

        watcher2 = ConfigWatcher(tmp_path)
        assert watcher2.get_previous_hash() == watcher1.compute_hash()

        watcher3 = ConfigWatcher(tmp_path)
        assert watcher3.has_changed() is False

        # Modify config
        config_file.write_text("modified config")
        assert watcher3.has_changed() is True

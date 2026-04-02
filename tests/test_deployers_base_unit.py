"""Unit tests for BaseDeployer helpers."""

from unittest.mock import MagicMock, patch

import pytest

from fraisier.deployers.api import APIDeployer


class TestDetectConfigChanges:
    def test_no_config_path_returns_false(self):
        deployer = APIDeployer({})
        result = deployer._detect_config_changes(config_path=None)
        assert result is False

    def test_config_unchanged_returns_false(self, tmp_path):
        config_path = tmp_path / "fraises.yaml"
        config_path.write_text("fraises: []")

        deployer = APIDeployer({})
        with patch("fraisier.config_watcher.ConfigWatcher") as MockWatcher:
            mock_watcher = MagicMock()
            mock_watcher.has_changed.return_value = False
            MockWatcher.return_value = mock_watcher

            result = deployer._detect_config_changes(config_path=config_path)

        assert result is False
        MockWatcher.assert_called_once_with(tmp_path)

    def test_config_changed_returns_true(self, tmp_path):
        config_path = tmp_path / "fraises.yaml"
        config_path.write_text("fraises: []")

        deployer = APIDeployer({})
        with patch("fraisier.config_watcher.ConfigWatcher") as MockWatcher:
            mock_watcher = MagicMock()
            mock_watcher.has_changed.return_value = True
            MockWatcher.return_value = mock_watcher

            result = deployer._detect_config_changes(config_path=config_path)

        assert result is True


class TestSyncFraisesYaml:
    def test_no_paths_logs_and_returns(self, tmp_path):
        deployer = APIDeployer({})
        # No-op: should not raise
        deployer._sync_fraises_yaml(source_path=None, dest_path=None)

    def test_missing_source_raises(self, tmp_path):
        deployer = APIDeployer({})
        with pytest.raises(FileNotFoundError):
            deployer._sync_fraises_yaml(
                source_path=tmp_path / "nonexistent.yaml",
                dest_path=tmp_path / "dest.yaml",
            )

    def test_existing_source_copies(self, tmp_path):
        source = tmp_path / "fraises.yaml"
        source.write_text("fraises: []")
        dest = tmp_path / "dest.yaml"

        deployer = APIDeployer({})
        mock_runner = MagicMock()
        mock_runner.run.return_value = MagicMock(ok=True)
        deployer.runner = mock_runner

        deployer._sync_fraises_yaml(source_path=source, dest_path=dest)
        mock_runner.run.assert_called_once()

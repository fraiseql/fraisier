"""Unit tests for BaseDeployer helpers."""

from unittest.mock import MagicMock, patch

import pytest

from fraisier.deployers.api import APIDeployer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deployer_with_runner(runner=None):
    deployer = APIDeployer({})
    if runner is not None:
        deployer.runner = runner
    return deployer


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
        assert mock_runner.run.call_count == 2
        calls = mock_runner.run.call_args_list
        assert calls[0][0][0] == ["mkdir", "-p", str(tmp_path)]
        assert calls[1][0][0] == ["cp", str(source), str(dest)]

    def test_creates_dest_directory_if_missing(self, tmp_path):
        source = tmp_path / "fraises.yaml"
        source.write_text("fraises: []")
        dest = tmp_path / "subdir" / "fraises.yaml"

        deployer = APIDeployer({})
        mock_runner = MagicMock()
        mock_runner.run.return_value = MagicMock(ok=True)
        deployer.runner = mock_runner

        deployer._sync_fraises_yaml(source_path=source, dest_path=dest)
        calls = mock_runner.run.call_args_list
        assert calls[0][0][0] == ["mkdir", "-p", str(tmp_path / "subdir")]


class TestInstallScaffold:
    """_install_scaffold() must pass cwd and -c config_path to the runner."""

    def test_install_scaffold_uses_cwd_and_config(self, tmp_path):
        """With config_path, command must include cd <dir> and -c <path>."""
        config_path = tmp_path / "fraises.yaml"
        config_path.touch()

        mock_runner = MagicMock()
        mock_runner.run.return_value = MagicMock(returncode=0, stdout="")

        deployer = _make_deployer_with_runner(mock_runner)
        deployer._install_scaffold(config_path=config_path)

        mock_runner.run.assert_called_once()
        cmd = mock_runner.run.call_args[0][0]
        assert f"cd {tmp_path}" in cmd[-1]
        assert f"-c {config_path}" in cmd[-1]

    def test_install_scaffold_no_config_falls_back(self):
        """Without config_path, original simple command is used."""
        mock_runner = MagicMock()
        mock_runner.run.return_value = MagicMock(returncode=0, stdout="")

        deployer = _make_deployer_with_runner(mock_runner)
        deployer._install_scaffold(config_path=None)

        mock_runner.run.assert_called_once()
        cmd = mock_runner.run.call_args[0][0]
        assert "scaffold-install" in " ".join(cmd)
        assert "cd " not in " ".join(cmd)

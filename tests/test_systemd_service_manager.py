"""Tests for SystemdServiceManager."""

from unittest.mock import MagicMock

import pytest

from fraisier.service_managers.systemd import SystemdServiceManager


class TestSystemdServiceManager:
    """Test SystemdServiceManager implementation."""

    @pytest.fixture
    def mock_runner(self):
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_runner):
        return SystemdServiceManager(mock_runner)

    def test_start_service(self, manager, mock_runner):
        manager.start("test_service")
        mock_runner.run.assert_called_once_with(
            ["sudo", "systemctl", "start", "test_service"], timeout=60, check=True
        )

    def test_stop_service(self, manager, mock_runner):
        manager.stop("test_service")
        mock_runner.run.assert_called_once_with(
            ["sudo", "systemctl", "stop", "test_service"], timeout=60, check=True
        )

    def test_restart_service(self, manager, mock_runner):
        manager.restart("test_service")
        mock_runner.run.assert_called_once_with(
            ["sudo", "systemctl", "restart", "test_service"], timeout=60, check=True
        )

    def test_is_active_service_active(self, manager, mock_runner):
        mock_runner.run.return_value.stdout = "active\n"
        assert manager.is_active("test_service") is True
        mock_runner.run.assert_called_once_with(
            ["sudo", "systemctl", "is-active", "test_service"], timeout=30, check=False
        )

    def test_is_active_service_inactive(self, manager, mock_runner):
        mock_runner.run.return_value.stdout = "inactive\n"
        assert manager.is_active("test_service") is False

    def test_daemon_reload(self, manager, mock_runner):
        manager.daemon_reload()
        mock_runner.run.assert_called_once_with(
            ["sudo", "systemctl", "daemon-reload"], timeout=60, check=True
        )

    def test_invalid_service_name(self, manager):
        with pytest.raises(ValueError, match="Invalid service name"):
            manager.start("invalid service name")

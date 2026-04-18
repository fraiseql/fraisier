"""Tests for RcServiceManager."""

from unittest.mock import MagicMock

import pytest

from fraisier.service_managers.rc import RcServiceManager


class TestRcServiceManager:
    """Test RcServiceManager implementation."""

    @pytest.fixture
    def mock_runner(self):
        return MagicMock()

    @pytest.fixture
    def manager(self, mock_runner):
        return RcServiceManager(mock_runner)

    def test_start_service(self, manager, mock_runner):
        manager.start("test_service")
        mock_runner.run.assert_called_once_with(
            ["service", "test_service", "start"], timeout=60, check=True
        )

    def test_stop_service(self, manager, mock_runner):
        manager.stop("test_service")
        mock_runner.run.assert_called_once_with(
            ["service", "test_service", "stop"], timeout=60, check=True
        )

    def test_restart_service(self, manager, mock_runner):
        manager.restart("test_service")
        mock_runner.run.assert_called_once_with(
            ["service", "test_service", "restart"], timeout=60, check=True
        )

    def test_is_active_service_running(self, manager, mock_runner):
        mock_runner.run.return_value.stdout = "test_service is running."
        assert manager.is_active("test_service") is True
        mock_runner.run.assert_called_once_with(
            ["service", "test_service", "onestatus"], timeout=30, check=False
        )

    def test_is_active_service_not_running(self, manager, mock_runner):
        mock_runner.run.return_value.stdout = "test_service is not running."
        assert manager.is_active("test_service") is False

    def test_daemon_reload(self, manager, mock_runner):
        manager.daemon_reload()
        mock_runner.run.assert_called_once_with(
            ["service", "rc", "reload"], timeout=60, check=True
        )

    def test_invalid_service_name(self, manager):
        with pytest.raises(ValueError, match="Invalid service name"):
            manager.start("invalid service name")

"""Tests for ServiceManager factory and detection."""

from unittest.mock import MagicMock, patch

import pytest

from fraisier.service_managers import get_service_manager
from fraisier.service_managers.rc import RcServiceManager
from fraisier.service_managers.systemd import SystemdServiceManager


class TestGetServiceManager:
    """Test get_service_manager factory function."""

    @pytest.fixture
    def mock_runner(self):
        return MagicMock()

    def test_linux_defaults_to_systemd(self, mock_runner):
        """On Linux, defaults to SystemdServiceManager."""
        with patch("platform.system", return_value="Linux"):
            manager = get_service_manager(mock_runner)
            assert isinstance(manager, SystemdServiceManager)

    def test_freebsd_defaults_to_rc(self, mock_runner):
        """On FreeBSD, defaults to RcServiceManager."""
        with patch("platform.system", return_value="FreeBSD"):
            manager = get_service_manager(mock_runner)
            assert isinstance(manager, RcServiceManager)

    def test_config_override_systemd(self, mock_runner):
        """Config can override to systemd."""
        config = {"service_manager": "systemd"}
        with patch("platform.system", return_value="FreeBSD"):
            manager = get_service_manager(mock_runner, config)
            assert isinstance(manager, SystemdServiceManager)

    def test_config_override_rc(self, mock_runner):
        """Config can override to rc."""
        config = {"service_manager": "rc"}
        with patch("platform.system", return_value="Linux"):
            manager = get_service_manager(mock_runner, config)
            assert isinstance(manager, RcServiceManager)

    def test_invalid_platform(self, mock_runner):
        """Unknown platform raises error."""
        with (
            patch("platform.system", return_value="Unknown"),
            pytest.raises(ValueError, match="Unsupported platform"),
        ):
            get_service_manager(mock_runner)

    def test_invalid_config_value(self, mock_runner):
        """Invalid service_manager in config raises error."""
        config = {"service_manager": "invalid"}
        with (
            patch("platform.system", return_value="Linux"),
            pytest.raises(ValueError, match="Invalid service_manager"),
        ):
            get_service_manager(mock_runner, config)

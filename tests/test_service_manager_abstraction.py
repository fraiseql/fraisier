from unittest.mock import patch

import pytest

from fraisier.service_managers.base import ServiceManager, ServiceManagerError


class MockServiceManager(ServiceManager):
    """Mock implementation for testing ServiceManager interface."""

    def __init__(self):
        self.started = set()
        self.stopped = set()
        self.restarted = set()

    def start(self, service_name: str) -> None:
        service_name = self._validate_service_name(service_name)
        self.started.add(service_name)

    def stop(self, service_name: str) -> None:
        service_name = self._validate_service_name(service_name)
        self.stopped.add(service_name)

    def restart(self, service_name: str) -> None:
        service_name = self._validate_service_name(service_name)
        self.restarted.add(service_name)

    def is_active(self, service_name: str) -> bool:
        service_name = self._validate_service_name(service_name)
        return service_name in self.started and service_name not in self.stopped

    def daemon_reload(self) -> None:
        pass


def test_service_manager_start():
    manager = MockServiceManager()
    manager.start("test_service")
    assert "test_service" in manager.started


def test_service_manager_stop():
    manager = MockServiceManager()
    manager.start("test_service")
    manager.stop("test_service")
    assert "test_service" in manager.stopped


def test_service_manager_restart():
    manager = MockServiceManager()
    manager.restart("test_service")
    assert "test_service" in manager.restarted


def test_service_manager_is_active():
    manager = MockServiceManager()
    assert not manager.is_active("test_service")
    manager.start("test_service")
    assert manager.is_active("test_service")
    manager.stop("test_service")
    assert not manager.is_active("test_service")


def test_service_manager_daemon_reload():
    manager = MockServiceManager()
    # Should not raise
    manager.daemon_reload()


def test_service_manager_invalid_service_name():
    manager = MockServiceManager()
    with pytest.raises(ValueError, match="Invalid service name"):
        manager.start("invalid service name")
    with pytest.raises(ValueError, match="Invalid service name"):
        manager.stop("invalid service name")
    with pytest.raises(ValueError, match="Invalid service name"):
        manager.restart("invalid service name")
    with pytest.raises(ValueError, match="Invalid service name"):
        manager.is_active("invalid service name")


def test_wait_stopped_success():
    manager = MockServiceManager()
    manager.start("test_service")  # Make it active

    with (
        patch.object(
            manager, "is_active", side_effect=[True, True, False]
        ) as mock_is_active,
        patch("time.sleep") as mock_sleep,
        patch("time.monotonic", return_value=0.0),
    ):
        manager.wait_stopped("test_service", timeout=5)

    assert mock_is_active.call_count == 3  # Initial check + two polls
    assert mock_sleep.call_count == 2


def test_wait_stopped_timeout():
    manager = MockServiceManager()
    manager.start("test_service")  # Make it active

    with (
        patch.object(manager, "is_active", return_value=True) as mock_is_active,
        patch("time.sleep") as mock_sleep,
        patch("time.monotonic", side_effect=[0.0, 1.0, 2.0, 6.0]),
        pytest.raises(
            ServiceManagerError, match="Service test_service still active after 5s"
        ),
    ):
        manager.wait_stopped("test_service", timeout=5)

    # Should have polled until timeout
    assert mock_is_active.call_count >= 1
    assert mock_sleep.call_count >= 1

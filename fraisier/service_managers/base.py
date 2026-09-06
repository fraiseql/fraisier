import logging
import re
import time
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9_@.\-]+$")


class ServiceManagerError(Exception):
    """Base exception for service manager operations."""


class ServiceManager(ABC):
    """Abstract service management interface."""

    def _validate_service_name(self, service_name: str) -> str:
        """Validate service name and return it.

        Raises:
            ValueError: If service_name is invalid.
        """
        if not _SERVICE_NAME_RE.match(service_name):
            raise ValueError(f"Invalid service name: {service_name!r}")
        return service_name

    @abstractmethod
    def start(self, service_name: str) -> None:
        """Start a service."""

    @abstractmethod
    def stop(self, service_name: str) -> None:
        """Stop a service."""

    @abstractmethod
    def restart(self, service_name: str) -> None:
        """Restart a service."""

    @abstractmethod
    def enable(self, service_name: str) -> None:
        """Enable a service so it starts on boot.

        Implementations must not escalate privileges themselves: the deploy
        and webhook units run with ``NoNewPrivileges``, under which ``sudo``
        exits 1 (#382).
        """

    @abstractmethod
    def is_active(self, service_name: str) -> bool:
        """Check if a service is active."""

    @abstractmethod
    def daemon_reload(self) -> None:
        """Reload the service daemon configuration."""

    def wait_stopped(
        self, service_name: str, timeout: int = 30, poll_interval: float = 0.5
    ) -> None:
        """Block until service is no longer active, or raise on timeout."""
        deadline = time.monotonic() + timeout
        while self.is_active(service_name):
            if time.monotonic() > deadline:
                raise ServiceManagerError(
                    f"Service {service_name} still active after {timeout}s"
                )
            time.sleep(poll_interval)

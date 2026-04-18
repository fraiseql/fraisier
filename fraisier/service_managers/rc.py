"""Rc.d service management for FreeBSD."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import ServiceManager

if TYPE_CHECKING:  # pragma: no cover
    from fraisier.runners import CommandRunner

logger = logging.getLogger(__name__)


class RcServiceManager(ServiceManager):
    """Manage rc.d services on FreeBSD.

    Uses the `service` command for all operations.
    """

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    def start(self, service_name: str, timeout: int = 60) -> None:
        """Start a rc.d service.

        Raises:
            ValueError: If service_name is invalid.
            subprocess.CalledProcessError: If service command fails.
        """
        service_name = self._validate_service_name(service_name)
        logger.info("Starting rc.d service: %s", service_name)
        cmd = ["service", service_name, "start"]
        self.runner.run(cmd, timeout=timeout, check=True)

    def stop(self, service_name: str, timeout: int = 60) -> None:
        """Stop a rc.d service.

        Raises:
            ValueError: If service_name is invalid.
            subprocess.CalledProcessError: If service command fails.
        """
        service_name = self._validate_service_name(service_name)
        logger.info("Stopping rc.d service: %s", service_name)
        cmd = ["service", service_name, "stop"]
        self.runner.run(cmd, timeout=timeout, check=True)

    def restart(self, service_name: str, timeout: int = 60) -> None:
        """Restart a rc.d service.

        Raises:
            ValueError: If service_name is invalid.
            subprocess.CalledProcessError: If service command fails.
        """
        service_name = self._validate_service_name(service_name)
        logger.info("Restarting rc.d service: %s", service_name)
        cmd = ["service", service_name, "restart"]
        self.runner.run(cmd, timeout=timeout, check=True)

    def is_active(self, service_name: str) -> bool:
        """Check if a rc.d service is active.

        Uses 'service <name> onestatus' and checks for 'is running'.

        Raises:
            ValueError: If service_name is invalid.
        """
        service_name = self._validate_service_name(service_name)
        cmd = ["service", service_name, "onestatus"]
        result = self.runner.run(cmd, timeout=30, check=False)
        return "is running" in result.stdout.lower()

    def daemon_reload(self, timeout: int = 60) -> None:
        """Reload rc.d scripts.

        Uses 'service rc reload' to reload the rc system.

        Raises:
            subprocess.CalledProcessError: If service command fails.
        """
        logger.info("Reloading rc.d scripts")
        cmd = ["service", "rc", "reload"]
        self.runner.run(cmd, timeout=timeout, check=True)

"""Rc.d service management for FreeBSD."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import ServiceManager

if TYPE_CHECKING:  # pragma: no cover
    from fraisier.runners import CommandRunner

logger = logging.getLogger(__name__)

_SYSTEMD_UNIT_SUFFIXES = (".service", ".timer", ".socket", ".target", ".path")


def _strip_unit_suffix(service_name: str) -> str:
    """Return *service_name* without its systemd unit-type suffix, if any."""
    for suffix in _SYSTEMD_UNIT_SUFFIXES:
        if service_name.endswith(suffix):
            return service_name[: -len(suffix)]
    return service_name


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

    def enable(self, service_name: str, timeout: int = 60) -> None:
        """Enable a rc.d service so it starts on boot.

        rc.d has no unit-type suffixes, so a systemd-style name such as
        ``backup.timer`` names the rc service ``backup``.

        Raises:
            ValueError: If service_name is invalid.
            subprocess.CalledProcessError: If sysrc fails.
        """
        service_name = self._validate_service_name(service_name)
        rc_name = _strip_unit_suffix(service_name)
        logger.info("Enabling rc.d service: %s", rc_name)
        cmd = ["sysrc", f"{rc_name}_enable=YES"]
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

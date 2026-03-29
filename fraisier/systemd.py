"""Systemd service management via CommandRunner."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fraisier.dbops._validation import validate_service_name

if TYPE_CHECKING:
    from fraisier.runners import CommandRunner

logger = logging.getLogger(__name__)


class SystemdServiceManager:
    """Manage systemd services through a CommandRunner."""

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    def stop(self, service_name: str, timeout: int = 60) -> None:
        """Stop a systemd service.

        Raises:
            ValueError: If service_name contains invalid characters.
            subprocess.CalledProcessError: If systemctl fails.
        """
        validate_service_name(service_name)
        self.runner.run(
            ["sudo", "systemctl", "stop", service_name],
            timeout=timeout,
            check=True,
        )

    def restart(self, service_name: str, timeout: int = 60) -> None:
        """Restart a systemd service.

        Raises:
            ValueError: If service_name contains invalid characters.
            subprocess.CalledProcessError: If systemctl fails.
        """
        validate_service_name(service_name)
        self.runner.run(
            ["sudo", "systemctl", "restart", service_name],
            timeout=timeout,
            check=True,
        )

    def status(self, service_name: str) -> str:
        """Return the active state of a systemd service (e.g. 'active', 'inactive').

        Raises:
            ValueError: If service_name contains invalid characters.
        """
        validate_service_name(service_name)
        result = self.runner.run(
            ["sudo", "systemctl", "is-active", service_name],
            timeout=30,
            check=False,
        )
        return result.stdout.strip()

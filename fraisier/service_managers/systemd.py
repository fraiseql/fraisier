"""Systemd service management via CommandRunner."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import types
from typing import TYPE_CHECKING

from .base import ServiceManager

if TYPE_CHECKING:  # pragma: no cover
    from fraisier.runners import CommandRunner

logger = logging.getLogger(__name__)


def _call_via_socket(
    socket_path: str,
    action: str,
    service_name: str = "",
    check: bool = True,
) -> types.SimpleNamespace:
    """Send a JSON command to the systemctl helper over a Unix socket.

    Args:
        socket_path: Absolute path to the Unix domain socket.
        action: systemctl action (stop, start, restart, is-active, daemon-reload).
        service_name: Service unit name (empty for daemon-reload).
        check: If True (default), raise on non-zero exit codes.  When False,
            return the result even if the helper reports ``ok=false`` (e.g.
            ``systemctl is-active`` returns exit code 3 for inactive services).

    Returns:
        A SimpleNamespace with ``.stdout``, ``.stderr``, ``.returncode`` attributes,
        mimicking :class:`subprocess.CompletedProcess`.

    Raises:
        ConnectionRefusedError: If the helper socket is not available.
        subprocess.CalledProcessError: If *check* is True and the helper
            returns ok=false.
    """
    request: dict[str, str] = {"action": action}
    if service_name:
        request["service"] = service_name

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(socket_path)
            sock.sendall(json.dumps(request).encode() + b"\n")
            with sock.makefile("rb") as f:
                raw = f.readline()
    except FileNotFoundError as exc:
        msg = f"systemctl helper socket not found: {socket_path}"
        raise ConnectionRefusedError(msg) from exc
    except OSError as exc:
        msg = f"Failed to connect to systemctl helper at {socket_path}: {exc}"
        raise ConnectionRefusedError(msg) from exc

    try:
        response = json.loads(raw.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = f"Malformed response from systemctl helper: {exc}"
        raise subprocess.CalledProcessError(1, action, stderr=msg) from exc

    result = types.SimpleNamespace(
        stdout=response.get("stdout", ""),
        stderr=response.get("stderr", ""),
        returncode=response.get("returncode", 0),
    )

    if check and not response.get("ok", False):
        error = response.get("error", "unknown error from systemctl helper")
        raise subprocess.CalledProcessError(
            result.returncode or 1,
            action,
            output=result.stdout,
            stderr=error,
        )

    return result


class SystemdServiceManager(ServiceManager):
    """Manage systemd services through a CommandRunner.

    Resolution order for systemctl calls:
    1. Unix socket (FRAISIER_SYSTEMCTL_SOCKET) — preferred, no privilege escalation
    2. Wrapper script (FRAISIER_SYSTEMCTL_WRAPPER) — legacy, requires sudo in sudoers
    3. sudo systemctl — fallback when neither env var is set
    """

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    def _run_systemctl(
        self,
        action: str,
        service_name: str = "",
        timeout: int = 60,
        check: bool = True,
    ) -> types.SimpleNamespace | subprocess.CompletedProcess[str]:
        """Run a systemctl command via socket, wrapper, or sudo.

        Returns the result mimicking subprocess.CompletedProcess.
        """
        socket_path = os.environ.get("FRAISIER_SYSTEMCTL_SOCKET")
        if socket_path:
            return _call_via_socket(socket_path, action, service_name, check=check)
        wrapper = os.environ.get("FRAISIER_SYSTEMCTL_WRAPPER")
        if wrapper:
            cmd = [wrapper, action] + ([service_name] if service_name else [])
        else:
            cmd = ["sudo", "systemctl", action] + (
                [service_name] if service_name else []
            )
        return self.runner.run(cmd, timeout=timeout, check=check)

    def start(self, service_name: str, timeout: int = 60) -> None:
        """Start a systemd service.

        Raises:
            ValueError: If service_name is invalid.
            subprocess.CalledProcessError: If systemctl fails.
        """
        service_name = self._validate_service_name(service_name)
        logger.info("Starting systemd service: %s", service_name)
        self._run_systemctl("start", service_name, timeout, check=True)

    def stop(self, service_name: str, timeout: int = 60) -> None:
        """Stop a systemd service.

        Raises:
            ValueError: If service_name is invalid.
            subprocess.CalledProcessError: If systemctl fails.
        """
        service_name = self._validate_service_name(service_name)
        logger.info("Stopping systemd service: %s", service_name)
        self._run_systemctl("stop", service_name, timeout, check=True)

    def restart(self, service_name: str, timeout: int = 60) -> None:
        """Restart a systemd service.

        Raises:
            ValueError: If service_name is invalid.
            subprocess.CalledProcessError: If systemctl fails.
        """
        service_name = self._validate_service_name(service_name)
        logger.info("Restarting systemd service: %s", service_name)
        self._run_systemctl("restart", service_name, timeout, check=True)

    def is_active(self, service_name: str) -> bool:
        """Check if a systemd service is active.

        Raises:
            ValueError: If service_name is invalid.
        """
        return self.status(service_name) == "active"

    def status(self, service_name: str) -> str:
        """Return the active state of a systemd service (e.g. 'active', 'inactive').

        Raises:
            ValueError: If service_name is invalid.
        """
        service_name = self._validate_service_name(service_name)
        result = self._run_systemctl("is-active", service_name, timeout=30, check=False)
        return result.stdout.strip()

    def daemon_reload(self, timeout: int = 60) -> None:
        """Run systemctl daemon-reload.

        Raises:
            subprocess.CalledProcessError: If systemctl fails.
        """
        logger.info("Running systemctl daemon-reload")
        self._run_systemctl("daemon-reload", timeout=timeout, check=True)

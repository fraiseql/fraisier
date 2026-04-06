"""Systemd service management via CommandRunner."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import types
from typing import TYPE_CHECKING

from fraisier.dbops._validation import validate_service_name

if TYPE_CHECKING:  # pragma: no cover
    from fraisier.runners import CommandRunner

logger = logging.getLogger(__name__)


def _call_via_socket(
    socket_path: str,
    action: str,
    service_name: str = "",
) -> types.SimpleNamespace:
    """Send a JSON command to the systemctl helper over a Unix socket.

    Args:
        socket_path: Absolute path to the Unix domain socket.
        action: systemctl action (stop, start, restart, is-active, daemon-reload).
        service_name: Service unit name (empty for daemon-reload).

    Returns:
        A SimpleNamespace with ``.stdout``, ``.stderr``, ``.returncode`` attributes,
        mimicking :class:`subprocess.CompletedProcess`.

    Raises:
        ConnectionRefusedError: If the helper socket is not available.
        subprocess.CalledProcessError: If the helper returns ok=false.
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

    if not response.get("ok", False):
        error = response.get("error", "unknown error from systemctl helper")
        raise subprocess.CalledProcessError(
            result.returncode or 1,
            action,
            output=result.stdout,
            stderr=error,
        )

    return result


class SystemdServiceManager:
    """Manage systemd services through a CommandRunner.

    Resolution order for systemctl calls:
    1. Unix socket (FRAISIER_SYSTEMCTL_SOCKET) — preferred, no privilege escalation
    2. Wrapper script (FRAISIER_SYSTEMCTL_WRAPPER) — legacy, requires sudo in sudoers
    3. sudo systemctl — fallback when neither env var is set
    """

    def __init__(self, runner: CommandRunner) -> None:
        self.runner = runner

    def stop(self, service_name: str, timeout: int = 60) -> None:  # pragma: no cover
        """Stop a systemd service.

        Raises:
            ValueError: If service_name contains invalid characters.
            subprocess.CalledProcessError: If systemctl fails.
        """
        validate_service_name(service_name)
        socket_path = os.environ.get("FRAISIER_SYSTEMCTL_SOCKET")
        if socket_path:
            _call_via_socket(socket_path, "stop", service_name)
            return
        wrapper = os.environ.get("FRAISIER_SYSTEMCTL_WRAPPER")
        if wrapper:
            cmd = [wrapper, "stop", service_name]
        else:
            cmd = ["sudo", "systemctl", "stop", service_name]
        self.runner.run(cmd, timeout=timeout, check=True)

    def restart(self, service_name: str, timeout: int = 60) -> None:
        """Restart a systemd service.

        Raises:
            ValueError: If service_name contains invalid characters.
            subprocess.CalledProcessError: If systemctl fails.
        """
        validate_service_name(service_name)
        socket_path = os.environ.get("FRAISIER_SYSTEMCTL_SOCKET")
        if socket_path:
            _call_via_socket(socket_path, "restart", service_name)
            return
        wrapper = os.environ.get("FRAISIER_SYSTEMCTL_WRAPPER")
        if wrapper:  # pragma: no cover
            cmd = [wrapper, "restart", service_name]
        else:
            cmd = ["sudo", "systemctl", "restart", service_name]
        self.runner.run(cmd, timeout=timeout, check=True)

    def status(self, service_name: str) -> str:
        """Return the active state of a systemd service (e.g. 'active', 'inactive').

        Raises:
            ValueError: If service_name contains invalid characters.
        """
        validate_service_name(service_name)
        socket_path = os.environ.get("FRAISIER_SYSTEMCTL_SOCKET")
        if socket_path:
            result = _call_via_socket(socket_path, "is-active", service_name)
            return result.stdout.strip()
        wrapper = os.environ.get("FRAISIER_SYSTEMCTL_WRAPPER")
        if wrapper:  # pragma: no cover
            cmd = [wrapper, "is-active", service_name]
        else:
            cmd = ["sudo", "systemctl", "is-active", service_name]
        result = self.runner.run(cmd, timeout=30, check=False)
        return result.stdout.strip()

    def daemon_reload(self, timeout: int = 60) -> None:
        """Run systemctl daemon-reload.

        Raises:
            subprocess.CalledProcessError: If systemctl fails.
        """
        socket_path = os.environ.get("FRAISIER_SYSTEMCTL_SOCKET")
        if socket_path:
            _call_via_socket(socket_path, "daemon-reload")
            return
        wrapper = os.environ.get("FRAISIER_SYSTEMCTL_WRAPPER")
        if wrapper:  # pragma: no cover
            cmd = [wrapper, "daemon-reload"]
        else:
            cmd = ["sudo", "systemctl", "daemon-reload"]
        self.runner.run(cmd, timeout=timeout, check=True)

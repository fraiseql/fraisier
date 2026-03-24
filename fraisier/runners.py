"""Command runner abstraction for local and remote execution.

Deployers use a ``CommandRunner`` to execute shell commands.  By default they
use ``LocalRunner`` (subprocess on the local machine).  When SSH configuration
is provided, ``SSHRunner`` routes commands through SSH to a remote host.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class CommandRunner(Protocol):
    """Protocol for executing shell commands."""

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 300,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]: ...


class LocalRunner:
    """Execute commands locally via subprocess."""

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 300,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )


class SSHRunner:
    """Execute commands on a remote host via SSH.

    Wraps each command invocation in an ``ssh`` call using the provided
    connection details.
    """

    def __init__(
        self,
        host: str,
        user: str = "root",
        port: int = 22,
        key_path: str | None = None,
        strict_host_key: bool = True,
    ) -> None:
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.strict_host_key = strict_host_key

    def _build_ssh_prefix(self) -> list[str]:
        """Build the SSH command prefix (everything before the remote cmd)."""
        host_key_policy = "accept-new" if self.strict_host_key else "no"
        prefix = [
            "ssh",
            "-o",
            f"StrictHostKeyChecking={host_key_policy}",
            "-o",
            "BatchMode=yes",
            "-p",
            str(self.port),
        ]
        if self.key_path:
            prefix.extend(["-i", self.key_path])
        prefix.append(f"{self.user}@{self.host}")
        return prefix

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 300,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        # Build the remote command string
        remote_cmd = shlex.join(cmd)
        if cwd:
            remote_cmd = f"cd {shlex.quote(cwd)} && {remote_cmd}"

        ssh_cmd = [*self._build_ssh_prefix(), remote_cmd]
        return subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )


def runner_from_config(
    ssh_config: dict[str, Any] | None = None,
) -> CommandRunner:
    """Create the appropriate runner from configuration.

    Args:
        ssh_config: Optional SSH connection details.  When provided,
            returns an ``SSHRunner``; otherwise a ``LocalRunner``.

    Returns:
        A CommandRunner instance.
    """
    if ssh_config:
        return SSHRunner(
            host=ssh_config["host"],
            user=ssh_config.get("user", "root"),
            port=ssh_config.get("port", 22),
            key_path=ssh_config.get("key_path"),
            strict_host_key=ssh_config.get("strict_host_key", True),
        )
    return LocalRunner()

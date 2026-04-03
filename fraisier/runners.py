"""Command runner abstraction for local and remote execution.

Deployers use a ``CommandRunner`` to execute shell commands.  By default they
use ``LocalRunner`` (subprocess on the local machine).  When SSH configuration
is provided, ``SSHRunner`` routes commands through SSH to a remote host.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

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
        env: dict[str, str] | None = None,
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
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
            env=env,
        )


class SSHRunner:
    """Execute commands on a remote host via SSH.

    Wraps each command invocation in an ``ssh`` call using the provided
    connection details.
    """

    _SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

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

    def _build_ssh_options(self) -> list[str]:
        """Build shared SSH/SCP options (host-key policy, batch mode, identity)."""
        host_key_policy = "accept-new" if self.strict_host_key else "no"
        opts = [
            "-o",
            f"StrictHostKeyChecking={host_key_policy}",
            "-o",
            "BatchMode=yes",
        ]
        if self.key_path:
            opts.extend(["-i", self.key_path])
        return opts

    def _build_ssh_prefix(self) -> list[str]:
        """Build the SSH command prefix (everything before the remote cmd)."""
        return [
            "ssh",
            *self._build_ssh_options(),
            "-p",
            str(self.port),
            f"{self.user}@{self.host}",
        ]

    def upload(
        self, local_path: Path, remote_path: str
    ) -> subprocess.CompletedProcess[str]:
        """Upload a single file to the remote host using scp."""
        scp_cmd = [
            "scp",
            *self._build_ssh_options(),
            "-P",
            str(self.port),
            str(local_path),
            f"{self.user}@{self.host}:{remote_path}",
        ]
        return subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )

    def upload_tree(self, local_dir: Path, remote_dir: str) -> None:
        """Upload a directory tree to the remote host via tar piped over SSH."""
        tar = subprocess.Popen(
            ["tar", "czf", "-", "-C", str(local_dir), "."],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        remote_cmd = (
            f"mkdir -p {shlex.quote(remote_dir)}"
            f" && tar xzf - -C {shlex.quote(remote_dir)}"
        )
        ssh_cmd = [*self._build_ssh_prefix(), remote_cmd]
        ssh_result = subprocess.run(
            ssh_cmd,
            stdin=tar.stdout,
            capture_output=True,
            check=False,
        )
        if tar.stdout:
            tar.stdout.close()
        _, tar_stderr = tar.communicate()

        if tar.returncode != 0:
            raise subprocess.CalledProcessError(
                tar.returncode, "tar", stderr=tar_stderr
            )
        if ssh_result.returncode != 0:
            raise subprocess.CalledProcessError(
                ssh_result.returncode, ssh_cmd, stderr=ssh_result.stderr
            )

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 300,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        # Build the remote command string; prepend env exports for SSH.
        # Always inject a safe PATH so sbin directories are available in
        # non-interactive SSH sessions (see #87).
        remote_cmd = shlex.join(cmd)
        merged_env = {"PATH": self._SAFE_PATH}
        if env:
            merged_env.update(env)
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in merged_env.items())
        remote_cmd = f"{exports} {remote_cmd}"
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

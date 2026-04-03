"""Command runner abstraction for local and remote execution.

Deployers use a ``CommandRunner`` to execute shell commands.  By default they
use ``LocalRunner`` (subprocess on the local machine).  When SSH configuration
is provided, ``SSHRunner`` routes commands through SSH to a remote host.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from pathlib import PurePosixPath
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
        use_sudo: bool = False,
        sudo_password: str | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.strict_host_key = strict_host_key
        self.use_sudo = use_sudo
        self.sudo_password = sudo_password

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
        """Upload a single file to the remote host using scp.

        When *use_sudo* is enabled, uploads to a temporary path first and
        then moves the file into place with ``sudo mv``, since scp itself
        cannot write to directories owned by root.
        """
        dest = remote_path
        if self.use_sudo:
            dest = f"/tmp/.fraisier-upload-{PurePosixPath(remote_path).name}"

        scp_cmd = [
            "scp",
            *self._build_ssh_options(),
            "-P",
            str(self.port),
            str(local_path),
            f"{self.user}@{self.host}:{dest}",
        ]
        result = subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        if self.use_sudo:
            self.run(["mv", dest, remote_path])
        return result

    def upload_tree(self, local_dir: Path, remote_dir: str) -> None:
        """Upload a directory tree to the remote host via tar piped over SSH.

        When *sudo_password* is set, uploads to a temporary directory first
        (without sudo), then moves into place with ``sudo -S``, since stdin
        cannot carry both the password and the tar stream simultaneously.
        """
        if self.use_sudo and self.sudo_password:
            return self._upload_tree_with_password(local_dir, remote_dir)

        tar = subprocess.Popen(
            ["tar", "czf", "-", "-C", str(local_dir), "."],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        remote_cmd = (
            f"mkdir -p {shlex.quote(remote_dir)}"
            f" && tar xzf - -C {shlex.quote(remote_dir)}"
        )
        if self.use_sudo:
            remote_cmd = f"sudo sh -c {shlex.quote(remote_cmd)}"
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

    def _upload_tree_with_password(self, local_dir: Path, remote_dir: str) -> None:
        """Upload tree via temp dir + sudo -S mv when password is needed."""
        tmp_dir = "/tmp/.fraisier-upload-tree"
        # Upload to temp dir without sudo
        tar = subprocess.Popen(
            ["tar", "czf", "-", "-C", str(local_dir), "."],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        remote_cmd = (
            f"mkdir -p {shlex.quote(tmp_dir)} && tar xzf - -C {shlex.quote(tmp_dir)}"
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
        # Move into place with sudo -S
        move_cmd = (
            f"mkdir -p {shlex.quote(remote_dir)}"
            f" && cp -a {shlex.quote(tmp_dir)}/. {shlex.quote(remote_dir)}/"
            f" && rm -rf {shlex.quote(tmp_dir)}"
        )
        self.run(["sh", "-c", move_cmd])

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
        if self.use_sudo:
            sudo_prefix = "sudo -S" if self.sudo_password else "sudo"
            remote_cmd = f"{sudo_prefix} sh -c {shlex.quote(remote_cmd)}"

        ssh_cmd = [*self._build_ssh_prefix(), remote_cmd]
        if self.sudo_password and self.use_sudo:
            return subprocess.run(
                ssh_cmd,
                input=self.sudo_password + "\n",
                capture_output=True,
                text=True,
                timeout=timeout,
                check=check,
            )
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

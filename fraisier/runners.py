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

from fraisier import ssh

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
        connect_timeout: int = 30,
        address_family: str | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.strict_host_key = strict_host_key
        self.use_sudo = use_sudo
        self.sudo_password = sudo_password
        self.connect_timeout = connect_timeout
        self.address_family = address_family
        # Single shared SshTarget — every SSH invocation in this runner
        # routes through fraisier.ssh, which carries the full defensive
        # flag set (BatchMode, ConnectTimeout, AddressFamily,
        # StrictHostKeyChecking, -n) by construction. This closes
        # LB-1/LB-2/LB-3/LB-5 from the Phase 1 inventory.
        self._target = ssh.SshTarget(
            host=host,
            user=user,
            port=port,
            key_path=key_path,
            strict_host_key=strict_host_key,
            connect_timeout=connect_timeout,
            address_family=address_family,
        )

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

        # Defensive flag set comes from ssh.scp_options — closes LB-7.
        scp_cmd = [
            "scp",
            *ssh.scp_options(self._target),
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

        remote_cmd = (
            f"mkdir -p {shlex.quote(remote_dir)}"
            f" && tar xzf - -C {shlex.quote(remote_dir)}"
        )
        if self.use_sudo:
            remote_cmd = f"sudo sh -c {shlex.quote(remote_cmd)}"
        self._tar_pipe_to_remote(local_dir, remote_cmd)

    def _upload_tree_with_password(self, local_dir: Path, remote_dir: str) -> None:
        """Upload tree via temp dir + sudo -S mv when password is needed."""
        tmp_dir = "/tmp/.fraisier-upload-tree"
        # Step 1: tar -> remote temp dir, no sudo (stdin is the tar stream).
        remote_cmd = (
            f"mkdir -p {shlex.quote(tmp_dir)} && tar xzf - -C {shlex.quote(tmp_dir)}"
        )
        self._tar_pipe_to_remote(local_dir, remote_cmd)
        # Step 2: sudo -S mv into place via the regular run() path.
        move_cmd = (
            f"mkdir -p {shlex.quote(remote_dir)}"
            f" && cp -a {shlex.quote(tmp_dir)}/. {shlex.quote(remote_dir)}/"
            f" && rm -rf {shlex.quote(tmp_dir)}"
        )
        self.run(["sh", "-c", move_cmd])

    def _tar_pipe_to_remote(self, local_dir: Path, remote_cmd: str) -> None:
        """Run ``tar czf - | ssh remote_cmd`` and raise on either failure.

        Routes the SSH leg through ``ssh.data_pipe`` so the defensive flag
        set (BatchMode, ConnectTimeout, AddressFamily, StrictHostKeyChecking)
        is applied — closing LB-5 from the Phase 1 inventory. ``-n`` is
        deliberately omitted by ``data_pipe`` because the tar stream rides
        on ssh's stdin.
        """
        tar = subprocess.Popen(
            ["tar", "czf", "-", "-C", str(local_dir), "."],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        ssh_result = ssh.data_pipe(
            self._target,
            [remote_cmd],
            stdin=tar.stdout,
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
                ssh_result.returncode,
                ssh_result.args,
                stderr=ssh_result.stderr,
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
        if self.use_sudo:
            sudo_prefix = "sudo -S" if self.sudo_password else "sudo"
            remote_cmd = f"{sudo_prefix} sh -c {shlex.quote(remote_cmd)}"

        # Pass the assembled remote shell-string as a single-element argv;
        # ssh.short_cmd's shlex.join wraps it in quotes which the remote
        # sh -c strips. The defensive flag set is applied by short_cmd
        # itself — see fraisier/ssh.py.
        if self.sudo_password and self.use_sudo:
            # `sudo -S` reads the password from stdin; ssh must therefore
            # NOT pass -n. cmd_with_input is the short-cmd shape minus -n.
            return ssh.cmd_with_input(
                self._target,
                [remote_cmd],
                input=self.sudo_password + "\n",
                timeout=timeout,
                check=check,
            )
        return ssh.short_cmd(
            self._target,
            [remote_cmd],
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
            connect_timeout=ssh_config.get("connect_timeout", 30),
            address_family=ssh_config.get("address_family"),
        )
    return LocalRunner()

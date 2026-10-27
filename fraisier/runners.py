"""Command runner abstraction for local and remote execution.

Deployers use a ``CommandRunner`` to execute shell commands.  By default they
use ``LocalRunner`` (subprocess on the local machine).  When SSH configuration
is provided, ``SSHRunner`` routes commands through SSH to a remote host.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import subprocess
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from fraisier import ssh
from fraisier.constants import DEFAULT_EXEC_TIMEOUT, SSH_CONNECT_TIMEOUT
from fraisier.dbops._pgconn import OPERATOR_DEFAULTS, scrubbed

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


#: Marks where the env payload starts on ``SSHRunner.run``'s stdin.
_ENV_SENTINEL = "__FRAISIER_ENV__"

#: Skips stdin up to the sentinel, then evals the rest. Skipping is what makes
#: an unconsumed sudo password line harmless: it is read and dropped, never run.
#: Exit 125 means stdin ended before the sentinel. No backslashes: fish, as a
#: login shell, rewrites them inside single quotes.
_ENV_READER = (
    "while :; do IFS= read -r l || exit 125; "
    f'[ "$l" = {_ENV_SENTINEL} ] && break; done; eval "$(cat)"; '
)

#: Unsets every ``PG*`` in the remote shell but the operator defaults, for
#: ``scrub_pg``. Runs before the payload is applied, so the payload's own
#: ``PG*`` stay. The names are filtered to shell names first: ``unset`` is a
#: special built-in, and an invalid name would end the shell.
_PG_SCRUB = (
    "for v in $(env | cut -d= -f1 | grep -E '^PG[A-Za-z0-9_]*$'); do "
    f"case $v in {'|'.join(sorted(OPERATOR_DEFAULTS))}) ;; "
    '*) unset "$v" ;; esac; done; '
)

#: Reads the sudo password line and checks it with ``sudo -v``, which reads
#: from a one-line pipe and so cannot retry into the env payload. Exit 124
#: means sudo refused it, 125 that stdin ended first. The ``printf`` format
#: ends in a literal newline, not a backslash escape (see ``_ENV_READER``).
_SUDO_VALIDATE = (
    "IFS= read -r p || exit 125; "
    "printf '%s\n' \"$p\" | sudo -S -p '' -v || exit 124; p=; "
)

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _env_payload(env: dict[str, str]) -> str:
    """The sentinel line, then one ``export`` line per key, values quoted.

    Keys are spliced in unquoted, so anything but a shell name is refused.
    """
    for key in env:
        if not _ENV_NAME.fullmatch(key):
            msg = f"not a valid environment variable name: {key!r}"
            raise ValueError(msg)
    exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items())
    return f"{_ENV_SENTINEL}\n{exports}"


@runtime_checkable
class CommandRunner(Protocol):
    """Protocol for executing shell commands.

    *env* holds only what the command needs on top of the runner's own
    environment. No runner puts a value on any argv: ``LocalRunner`` passes it
    as the child's environment and ``SSHRunner`` sends it on the remote
    command's stdin. A caller still passes only the few keys it means, never a
    copy of ``os.environ``: on a remote host the operator's environment is
    meaningless, and every key is shipped to it.

    *scrub_pg* is for a libpq child whose connection *env* names fully: every
    ``PG*`` in the environment it runs in, but the operator defaults, is
    dropped before *env* is applied (D13).
    """

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: int = DEFAULT_EXEC_TIMEOUT,
        check: bool = True,
        env: dict[str, str] | None = None,
        scrub_pg: bool = False,
    ) -> subprocess.CompletedProcess[str]: ...


class LocalRunner:
    """Execute commands locally via subprocess."""

    def run(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout: int = DEFAULT_EXEC_TIMEOUT,
        check: bool = True,
        env: dict[str, str] | None = None,
        scrub_pg: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        base = scrubbed(os.environ) if scrub_pg else os.environ
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
            env={**base, **(env or {})} if env or scrub_pg else None,
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
        connect_timeout: int = SSH_CONNECT_TIMEOUT,
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
        # StrictHostKeyChecking) by construction. This closes
        # LB-1/LB-3/LB-5 from the SSH hardening inventory; run() closes
        # LB-2 by closing its stdin after the env payload instead of -n.
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
            timeout=DEFAULT_EXEC_TIMEOUT,
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

        # --no-same-owner: root's tar otherwise keeps the archive's numeric
        # owners, so the operator's local uid would own what root runs.
        remote_cmd = (
            f"mkdir -p {shlex.quote(remote_dir)}"
            f" && tar xzf - --no-same-owner -C {shlex.quote(remote_dir)}"
        )
        if self.use_sudo:
            remote_cmd = f"sudo sh -c {shlex.quote(remote_cmd)}"
        self._tar_pipe_to_remote(local_dir, remote_cmd)

    def _upload_tree_with_password(self, local_dir: Path, remote_dir: str) -> None:
        """Upload tree via temp dir + sudo -S mv when password is needed."""
        import uuid

        # Randomise the tmp path to avoid collisions between concurrent uploads.
        tmp_dir = f"/tmp/.fraisier-upload-{uuid.uuid4().hex[:12]}"
        # Step 1: tar -> remote temp dir, no sudo (stdin is the tar stream).
        remote_cmd = (
            f"mkdir -p {shlex.quote(tmp_dir)} && tar xzf - -C {shlex.quote(tmp_dir)}"
        )
        try:
            self._tar_pipe_to_remote(local_dir, remote_cmd)
        except Exception:
            # Best-effort remote cleanup; ignore secondary errors.
            with contextlib.suppress(Exception):
                self.run(["rm", "-rf", tmp_dir], check=False)
            raise
        # Step 2: sudo -S copy into place via the regular run() path. `cp -R`,
        # not `cp -a`: root then owns the copies, where -a would keep the SSH
        # user as the owner of files root may later execute.
        move_cmd = (
            f"mkdir -p {shlex.quote(remote_dir)}"
            f" && cp -R {shlex.quote(tmp_dir)}/. {shlex.quote(remote_dir)}/"
            f" && rm -rf {shlex.quote(tmp_dir)}"
        )
        self.run(["sh", "-c", move_cmd])

    def _tar_pipe_to_remote(self, local_dir: Path, remote_cmd: str) -> None:
        """Run ``tar czf - | ssh remote_cmd`` and raise on either failure.

        Routes the SSH leg through ``ssh.data_pipe`` so the defensive flag
        set (BatchMode, ConnectTimeout, AddressFamily, StrictHostKeyChecking)
        is applied — closing LB-5 from the SSH hardening inventory. ``-n`` is
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
            ["sh", "-c", remote_cmd],
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
        timeout: int = DEFAULT_EXEC_TIMEOUT,
        check: bool = True,
        env: dict[str, str] | None = None,
        scrub_pg: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        # The command goes on argv; env values go on stdin, after a sentinel
        # line, as `export` lines the remote shell evals (#492). Always
        # inject a safe PATH so sbin directories are available in
        # non-interactive SSH sessions (see #87).
        payload = _env_payload({"PATH": self._SAFE_PATH, **(env or {})})
        script = (_PG_SCRUB if scrub_pg else "") + _ENV_READER
        script += f"cd {shlex.quote(cwd)} && " if cwd else ""
        script += f"exec {shlex.join(cmd)}"
        # fraisier.ssh shlex.joins the argv it is given and sshd hands the
        # result to the login shell, so the script goes in as `sh -c`'s one
        # argument. A one-element argv was quoted into a single word, which
        # the login shell read as a command name (#494).
        remote_argv = ["sh", "-c", script]
        if self.use_sudo and self.sudo_password:
            # The password is the first line, checked on a pipe of its own
            # before anything reads the env payload (D7). The inner sudo is
            # not exec'd: its tty-less ticket belongs to this shell.
            remote_argv = [
                "sh",
                "-c",
                f"{_SUDO_VALIDATE}sudo -n {shlex.join(remote_argv)}",
            ]
            payload = f"{self.sudo_password}\n{payload}"
        elif self.use_sudo:
            remote_argv = ["sudo", *remote_argv]

        # Without -n: the payload rides on stdin, and subprocess.run closes
        # the pipe after it, so the command still reads EOF (LB-2).
        return ssh.cmd_with_input(
            self._target,
            remote_argv,
            input=payload,
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
            connect_timeout=ssh_config.get("connect_timeout", SSH_CONNECT_TIMEOUT),
            address_family=ssh_config.get("address_family"),
        )
    return LocalRunner()

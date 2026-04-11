"""Centralised SSH invocation abstraction.

Every direct subprocess-based SSH call in fraisier should go through this
module. The three top-level entry points encode the patterns identified in
Phase 1 of the SSH I/O contract refactor
(see ``.phases/2026-04-10-ssh-io-contract/inventory.md``):

- :func:`short_cmd`   — run a remote command and capture output
- :func:`long_stream` — tail a remote process; caller owns the ``Popen``
- :func:`data_pipe`   — feed a local byte stream into SSH stdin

Each entry point applies the full defensive flag set by construction. The
flags exist because of specific production failures; the history is in
``cli/logs.py:_build_ssh_cmd`` and in
``.phases/2026-04-10-ssh-io-contract/inventory.md`` ("Per-flag rationale").
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SshTarget:
    """Connection parameters for a single SSH destination.

    Instances are frozen so a target built at config-load time can be
    passed around the deployer without anyone mutating it mid-deploy.

    Attributes:
        host: Destination hostname or IP.
        user: Remote user (default ``root``).
        port: TCP port (default ``22``).
        key_path: Path to the identity file, or ``None`` to use the
            agent / default keys.
        strict_host_key: When ``True`` (default), ``StrictHostKeyChecking``
            is set to ``accept-new``; when ``False``, to ``no``.
        connect_timeout: ``-o ConnectTimeout=`` value in seconds (default
            ``30``). Exists because on dual-stack hosts SSH tries AAAA
            first and waits for the kernel TCP timeout (~2 min) without
            this option. See commit ``4dd1927``.
        address_family: Optional ``-o AddressFamily=`` value — ``"inet"``
            or ``"inet6"``. Exists because operators on IPv6-broken
            networks need to pin IPv4. See commit ``64f8d30``.
    """

    host: str
    user: str = "root"
    port: int = 22
    key_path: str | None = None
    strict_host_key: bool = True
    connect_timeout: int = 30
    address_family: str | None = None

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> SshTarget:
        """Build an ``SshTarget`` from a fraise ``ssh:`` config dict.

        The dict shape matches what every existing call site already
        consumes (``logs.py``, ``runners.py``, ``validation.py``,
        ``bare_metal.py``) — keeps the migration in Phase 3 mechanical.

        Raises:
            KeyError: when ``host`` is missing from the dict.
        """
        return cls(
            host=cfg["host"],
            user=cfg.get("user", "root"),
            port=cfg.get("port", 22),
            key_path=cfg.get("key_path"),
            strict_host_key=cfg.get("strict_host_key", True),
            connect_timeout=cfg.get("connect_timeout", 30),
            address_family=cfg.get("address_family"),
        )

    def _options(self) -> list[str]:
        """Return the shared ``-o ...`` block (plus ``-i`` when a key is
        configured). Does NOT include ``-p``/``-P`` (port is set by the
        specific entry point, since ``ssh`` uses ``-p`` and ``scp`` uses
        ``-P``) and does NOT include ``-n`` (that is stdin-pattern
        dependent — see :func:`short_cmd` vs :func:`data_pipe`).
        """
        host_key_policy = "accept-new" if self.strict_host_key else "no"
        opts: list[str] = [
            # Why: BatchMode=yes — never prompt for passphrase/password;
            # fail fast instead of blocking on a closed TTY.
            "-o",
            "BatchMode=yes",
            "-o",
            f"StrictHostKeyChecking={host_key_policy}",
            # Why: commit 4dd1927 — without ConnectTimeout, SSH waits
            # ~2 min for the kernel TCP timeout before falling back from
            # an unreachable AAAA to a reachable A record.
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
        ]
        # Why: commit 64f8d30 — AddressFamily lets operators pin
        # IPv4/IPv6 on hosts where the other family is unreachable.
        if self.address_family is not None:
            opts.extend(["-o", f"AddressFamily={self.address_family}"])
        if self.key_path is not None:
            opts.extend(["-i", self.key_path])
        return opts

    def _ssh_argv(self, *, include_dash_n: bool) -> list[str]:
        """Build the full ``ssh ... user@host`` prefix for a given stdin
        pattern. ``include_dash_n=True`` is the right default for every
        pattern except :func:`data_pipe`, which legitimately feeds stdin.
        """
        argv: list[str] = ["ssh"]
        if include_dash_n:
            # Why: commit da5c119 — without -n, SSH still allocates a
            # stdin channel and hangs for minutes in non-interactive
            # contexts. Must NOT be set by data_pipe (stdin is the data).
            argv.append("-n")
        argv.extend(self._options())
        argv.extend(["-p", str(self.port)])
        argv.append(f"{self.user}@{self.host}")
        return argv


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def short_cmd(
    target: SshTarget,
    remote_argv: list[str],
    *,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a short remote command and capture output.

    This is the default pattern — the parent never writes to SSH stdin,
    so ``-n`` is always set. Output is captured as text.

    Args:
        target: Destination.
        remote_argv: The remote command as an argv list. It is
            shell-joined (``shlex.join``) before being passed to SSH,
            which takes a single remote-command string.
        timeout: Wall-clock timeout in seconds (default 60).
        check: Raise ``CalledProcessError`` on non-zero exit (default True).

    Raises:
        subprocess.TimeoutExpired: when the command outlives ``timeout``.
        subprocess.CalledProcessError: on non-zero exit when ``check``.
    """
    ssh_argv = [*target._ssh_argv(include_dash_n=True), shlex.join(remote_argv)]
    return subprocess.run(
        ssh_argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def long_stream(
    target: SshTarget,
    remote_argv: list[str],
) -> subprocess.Popen[bytes]:
    """Start a long-running remote stream.

    The returned ``Popen`` is owned by the caller — typical usage is to
    wait on it and forward SIGINT::

        proc = long_stream(target, ["journalctl", "--no-pager", "-f"])
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()

    The discipline here is historically brittle; each knob below maps to
    a specific production incident (see
    ``.phases/2026-04-10-ssh-io-contract/inventory.md`` → "Per-flag
    rationale"):

    - Popen (not ``os.execvp``), so the parent can wait/signal. Commit
      ``8fc8fec``.
    - ``stdin=DEVNULL`` at the Popen call, so SSH doesn't hold the
      connection open waiting for an inherited pipe's EOF. Commit
      ``08265c9``.
    - ``-n`` on the ssh argv, as a belt-and-braces guard that SSH never
      allocates a stdin channel. Commit ``da5c119``.
    - stdout/stderr inherited (unset), so TTY behaviour survives.
    """
    ssh_argv = [*target._ssh_argv(include_dash_n=True), shlex.join(remote_argv)]
    return subprocess.Popen(ssh_argv, stdin=subprocess.DEVNULL)


def scp_options(target: SshTarget) -> list[str]:
    """Return the shared flag block for an ``scp`` invocation.

    ``scp`` accepts the same ``-o`` flags as ``ssh`` (it *is* ssh under
    the hood); the only difference is ``-P`` (capital) instead of ``-p``
    for the port. This helper returns the defensive ``-o`` block from
    :meth:`SshTarget._options` followed by ``-P <port>`` so the caller
    only has to prepend ``"scp"`` and append ``src``/``dest``.

    Closes LB-7 in ``.phases/2026-04-10-ssh-io-contract/latent-bugs.md``:
    every scp upload now carries ``ConnectTimeout``/``AddressFamily`` and
    is no longer vulnerable to the IPv6-fallback hang.

    The ``-n`` flag is deliberately omitted: scp does not accept it, and
    scp's stdin is unused anyway.
    """
    return [*target._options(), "-P", str(target.port)]


def cmd_with_input(
    target: SshTarget,
    remote_argv: list[str],
    *,
    input: str,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a remote command and pipe a small text payload to its stdin.

    Same shape as :func:`short_cmd` (text-mode capture, defensive flag
    set, exit-code semantics) except that the parent feeds ``input`` to
    SSH's stdin via ``subprocess.run(input=...)``. The motivating use
    case is ``sudo -S`` — SSHRunner needs to pipe the sudo password to
    the remote sudo while still capturing stdout/stderr as text.

    ``-n`` MUST be omitted here: ``-n`` redirects ssh's own stdin from
    /dev/null, so the password would never reach the remote process.
    Every other defensive flag (``BatchMode``, ``ConnectTimeout``,
    ``StrictHostKeyChecking``, ``AddressFamily``) still applies — the
    only difference from ``short_cmd`` is the missing ``-n``.

    Args:
        target: Destination.
        remote_argv: The remote command as an argv list. Shell-joined
            before being passed to SSH.
        input: Text payload written to the SSH stdin (and forwarded to
            the remote process). Pass-through to ``subprocess.run``.
        timeout: Wall-clock timeout in seconds.
        check: Raise ``CalledProcessError`` on non-zero exit.

    Raises:
        subprocess.TimeoutExpired: when the command outlives ``timeout``.
        subprocess.CalledProcessError: on non-zero exit when ``check``.
    """
    ssh_argv = [*target._ssh_argv(include_dash_n=False), shlex.join(remote_argv)]
    return subprocess.run(
        ssh_argv,
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def data_pipe(
    target: SshTarget,
    remote_argv: list[str],
    stdin: Any,
    *,
    timeout: int = 300,
) -> subprocess.CompletedProcess[bytes]:
    """Run a remote command with caller-supplied stdin.

    This is the ``upload_tree`` shape: the parent opens a local producer
    subprocess (typically ``tar czf -``) and hands its ``stdout`` to SSH
    as ``stdin``. ``stdin`` is anything ``subprocess.run`` accepts in its
    ``stdin=`` kwarg — a file descriptor, a file object, or a
    ``Popen.stdout`` pipe.

    Unlike :func:`short_cmd` / :func:`long_stream`, ``-n`` must NOT be
    set — SSH must read stdin for the stream. Every other defensive
    flag (``BatchMode``, ``ConnectTimeout``, ``StrictHostKeyChecking``,
    ``AddressFamily``) still applies; LB-5 in ``latent-bugs.md`` is
    closed by this.
    """
    ssh_argv = [*target._ssh_argv(include_dash_n=False), shlex.join(remote_argv)]
    return subprocess.run(
        ssh_argv,
        stdin=stdin,
        capture_output=True,
        timeout=timeout,
        check=False,
    )

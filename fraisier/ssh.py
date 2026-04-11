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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import subprocess


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


# ---------------------------------------------------------------------------
# Entry points (implemented in later cycles of Phase 2)
# ---------------------------------------------------------------------------


def short_cmd(
    target: SshTarget,
    remote_argv: list[str],
    *,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a short remote command and capture output. (Cycle 2.)"""
    raise NotImplementedError


def long_stream(
    target: SshTarget,
    remote_argv: list[str],
) -> subprocess.Popen[bytes]:
    """Start a long-running remote stream. (Cycle 4.)"""
    raise NotImplementedError


def data_pipe(
    target: SshTarget,
    remote_argv: list[str],
    stdin: int,
    *,
    timeout: int = 300,
) -> subprocess.CompletedProcess[bytes]:
    """Run a remote command with caller-supplied stdin. (Cycle 5.)"""
    raise NotImplementedError

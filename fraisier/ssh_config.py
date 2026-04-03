"""Resolve SSH connection parameters from ~/.ssh/config via OpenSSH."""

from __future__ import annotations

import contextlib
import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SSHHostConfig:
    """Resolved SSH settings for a given hostname."""

    user: str | None = None
    port: int | None = None
    identity_file: str | None = None


def resolve_ssh_config(hostname: str) -> SSHHostConfig:
    """Resolve effective SSH settings for *hostname* using ``ssh -G``.

    Runs ``ssh -G <hostname>`` which outputs the fully-resolved configuration
    that OpenSSH would use for the given host.  This respects ``~/.ssh/config``
    entries including ``Host``, ``Match``, ``Include``, wildcards, etc.

    Returns an :class:`SSHHostConfig` with the resolved values, or an empty
    config (all ``None``) if resolution fails (e.g. ``ssh`` not found).
    """
    try:
        result = subprocess.run(
            ["ssh", "-G", hostname],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        logger.debug("ssh -G failed: %s", e)
        return SSHHostConfig()

    if result.returncode != 0:
        logger.debug(
            "ssh -G %s returned %d: %s", hostname, result.returncode, result.stderr
        )
        return SSHHostConfig()

    return _parse_ssh_g_output(result.stdout)


def _parse_ssh_g_output(output: str) -> SSHHostConfig:
    """Parse the key/value output of ``ssh -G``."""
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None

    for line in output.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].lower(), parts[1]

        if key == "user":
            user = value
        elif key == "port":
            with contextlib.suppress(ValueError):
                port = int(value)
        elif key == "identityfile" and not value.startswith("~/.ssh/id_"):
            # ssh -G always outputs default identity files (~/.ssh/id_rsa,
            # ~/.ssh/id_ecdsa, etc.) even when no IdentityFile is configured.
            # We only capture explicitly-configured identity files.
            identity_file = value

    return SSHHostConfig(user=user, port=port, identity_file=identity_file)

"""``SO_PEERCRED`` enforcement shared by every fraisier socket helper.

Trust-model context
-------------------

Pre-v0.29 helpers (``systemctl-helper``, ``scaffold-install-helper``,
``install-helper``) gated access purely via the socket file's filesystem
permissions (``SocketUser=root, SocketGroup=<deploy_user>,
SocketMode=0660``). Their docstrings implied a peer-credentials check that
the code never performed. From v0.29 onward, every helper calls
``check_peer_creds`` immediately after ``accept()`` and rejects any
connection whose peer UID does not match the configured ``deploy_user``.

The expected UID is passed numerically via ``--deploy-uid <N>`` baked into
the unit's ``ExecStart`` at scaffold-render time — so the helper has no
NSS dependency and is not subject to ``/etc/passwd`` surprises.

This module performs no IO at import time and has no side effects.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

_UCRED_FORMAT = "iII"  # struct ucred { pid_t pid; uid_t uid; gid_t gid; }
_UCRED_SIZE = struct.calcsize(_UCRED_FORMAT)


@dataclass(frozen=True)
class PeerCreds:
    """Peer credentials read via ``SO_PEERCRED``."""

    pid: int
    uid: int
    gid: int


def extract_deploy_uid(argv: list[str]) -> tuple[int | None, list[str]]:
    """Pull the deploy-uid out of ``argv``; return ``(uid, remaining_argv)``.

    Accepts either:

    - ``--deploy-uid <N>`` (numeric override; useful for tests + already-known
      UID); or
    - ``--deploy-user <name>`` (preferred — resolved at helper startup via
      ``pwd.getpwnam``). Deferring resolution to the target host avoids
      requiring the user to exist on whichever machine ran ``fraisier
      scaffold``. The renderer emits this form.

    No flag, malformed flag, missing user, non-integer UID → ``(None, argv)``.
    The caller logs a transitional warning and runs without ``SO_PEERCRED``
    enforcement (v0.30 will make the flag mandatory).
    """
    remaining = list(argv)
    uid = _pop_uid_flag(remaining)
    if uid is not None:
        return uid
    return _pop_user_flag(remaining)


def _pop_uid_flag(argv: list[str]) -> tuple[int, list[str]] | None:
    if "--deploy-uid" not in argv:
        return None
    i = argv.index("--deploy-uid")
    if i + 1 >= len(argv):
        return None
    try:
        uid = int(argv[i + 1])
    except ValueError:
        return None
    return uid, argv[:i] + argv[i + 2 :]


def _pop_user_flag(argv: list[str]) -> tuple[int | None, list[str]]:
    if "--deploy-user" not in argv:
        return None, argv
    i = argv.index("--deploy-user")
    if i + 1 >= len(argv):
        return None, argv
    name = argv[i + 1]
    try:
        import pwd

        uid = pwd.getpwnam(name).pw_uid
    except KeyError:
        return None, argv
    return uid, argv[:i] + argv[i + 2 :]


def check_peer_creds(conn: socket.socket, *, expected_uid: int) -> PeerCreds:
    """Read ``SO_PEERCRED`` on ``conn`` and reject if peer UID differs.

    Args:
        conn: Connected Unix socket.
        expected_uid: Numeric UID of the configured ``deploy_user``.

    Returns:
        ``PeerCreds(pid, uid, gid)`` — the peer's credentials.

    Raises:
        PermissionError: If the peer's UID does not match ``expected_uid``.
            The exception message names the offending UID for log analysis.
    """
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED_SIZE)
    pid, uid, gid = struct.unpack(_UCRED_FORMAT, raw)
    if uid != expected_uid:
        msg = (
            f"connection from uid={uid} (pid={pid}) rejected; "
            f"expected uid={expected_uid}"
        )
        raise PermissionError(msg)
    return PeerCreds(pid=pid, uid=uid, gid=gid)

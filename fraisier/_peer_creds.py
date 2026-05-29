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

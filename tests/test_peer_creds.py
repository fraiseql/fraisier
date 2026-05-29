"""Tests for ``fraisier._peer_creds.check_peer_creds``.

The check itself is enforced via ``getsockopt(SO_PEERCRED, ...)`` on a
connected Unix socket. We test against ``socket.socketpair()`` — both ends
run as the test process's UID, so we exercise the matching case with
``os.getuid()`` and the rejecting case with a deliberately-wrong UID.
"""

from __future__ import annotations

import os
import socket

import pytest

from fraisier._peer_creds import check_peer_creds


def test_check_peer_creds_accepts_matching_uid() -> None:
    """Matching UID: returns the (pid, uid, gid) of the peer with no raise."""
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        creds = check_peer_creds(server, expected_uid=os.getuid())
        assert creds.uid == os.getuid()
        assert creds.gid == os.getgid()
        assert creds.pid > 0
    finally:
        server.close()
        client.close()


def test_check_peer_creds_rejects_non_matching_uid() -> None:
    """Wrong expected_uid: PermissionError naming the offending peer UID."""
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    wrong_uid = os.getuid() + 1
    try:
        with pytest.raises(PermissionError, match=str(os.getuid())):
            check_peer_creds(server, expected_uid=wrong_uid)
    finally:
        server.close()
        client.close()

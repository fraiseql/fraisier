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

from fraisier._peer_creds import check_peer_creds, extract_deploy_uid


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


# ---------------------------------------------------------------------------
# extract_deploy_uid
# ---------------------------------------------------------------------------


def test_extract_deploy_uid_pulls_flag_and_value() -> None:
    uid, remaining = extract_deploy_uid(["--deploy-uid", "1001", "foo", "bar"])
    assert uid == 1001
    assert remaining == ["foo", "bar"]


def test_extract_deploy_uid_handles_flag_in_middle() -> None:
    uid, remaining = extract_deploy_uid(["foo", "--deploy-uid", "42", "bar"])
    assert uid == 42
    assert remaining == ["foo", "bar"]


def test_extract_deploy_uid_returns_none_when_absent() -> None:
    """Transitional fallback: pre-v0.29 units have no --deploy-uid."""
    uid, remaining = extract_deploy_uid(["foo", "bar"])
    assert uid is None
    assert remaining == ["foo", "bar"]


def test_extract_deploy_uid_returns_none_on_malformed_value() -> None:
    """Non-integer value → treated as missing (fall back to old behaviour)."""
    uid, remaining = extract_deploy_uid(["--deploy-uid", "not-a-number", "foo"])
    assert uid is None
    assert remaining == ["--deploy-uid", "not-a-number", "foo"]


def test_extract_deploy_uid_returns_none_when_trailing_flag() -> None:
    uid, remaining = extract_deploy_uid(["foo", "--deploy-uid"])
    assert uid is None
    assert remaining == ["foo", "--deploy-uid"]

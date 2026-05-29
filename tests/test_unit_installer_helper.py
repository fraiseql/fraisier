"""Tests for fraisier.unit_installer_helper — root-privileged manifest helper.

Bundle A #240 Phase 4. Tests exercise the helper against ``socket.socketpair``
fixtures (no real systemd socket activation). The helper reads one manifest
per connection, validates, executes, and writes a structured response.
"""

from __future__ import annotations

import json
import os
import socket
from typing import TYPE_CHECKING

import pytest

from fraisier.unit_installer_helper import (
    _handle_manifest,
    _parse_allowlist,
    _serve_connection,
)
from fraisier.unit_installer_protocol import (
    Allowlist,
    AllowlistEntry,
    DaemonReloadAction,
    InstallFileOp,
    Manifest,
    serialize_manifest,
)

if TYPE_CHECKING:
    from pathlib import Path


def _socket_pair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)


def _recv_json(sock: socket.socket) -> dict:
    with sock.makefile("rb") as f:
        raw = f.readline()
    return json.loads(raw.decode())


def _seed_layout(tmp_path: Path) -> tuple[Path, Path]:
    src_dir = tmp_path / "app" / "scripts" / "systemd"
    dest_dir = tmp_path / "etc" / "systemd" / "system"
    src_dir.mkdir(parents=True)
    dest_dir.mkdir(parents=True)
    return src_dir, dest_dir


def _allowlist_for(src_dir: Path, dest_dir: Path) -> Allowlist:
    return Allowlist(
        entries=(AllowlistEntry(source_prefix=src_dir, dest_prefix=dest_dir),)
    )


# ---------------------------------------------------------------------------
# Cycle 4.1 — helper reads + dispatches a manifest from a socketpair
# Cycle 4.3 — single install_file op writes the file, mode 0644
# ---------------------------------------------------------------------------


def test_handle_manifest_writes_install_file_op_and_replies_ok(
    tmp_path: Path,
) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    source = src_dir / "foo.timer"
    source.write_text("[Unit]\n")
    op = InstallFileOp(
        source_path=str(source),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
    )
    manifest = Manifest(version=1, deploy_id="t1", operations=(op,))

    server, client = _socket_pair()
    client.sendall(serialize_manifest(manifest))
    client.shutdown(socket.SHUT_WR)
    _handle_manifest(server, allowlist=_allowlist_for(src_dir, dest_dir))
    response = _recv_json(client)

    assert response["status"] == "ok"
    assert "foo.timer" in response["installed"]
    final = dest_dir / "foo.timer"
    assert final.exists()
    assert final.read_text() == "[Unit]\n"
    assert (final.stat().st_mode & 0o777) == 0o644


# ---------------------------------------------------------------------------
# Cycle 4.2 — peer-creds rejection wired up
# ---------------------------------------------------------------------------


def test_serve_connection_rejects_wrong_uid_peer(tmp_path: Path) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    server, client = _socket_pair()
    wrong_uid = os.getuid() + 1
    _serve_connection(
        server,
        expected_uid=wrong_uid,
        allowlist=_allowlist_for(src_dir, dest_dir),
    )
    response = _recv_json(client)
    assert response["status"] == "rejected"
    assert "peer" in response["reason"].lower()


def test_serve_connection_with_matching_uid_dispatches(tmp_path: Path) -> None:
    """When peer UID matches, the manifest is processed end-to-end."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    source = src_dir / "foo.timer"
    source.write_text("x")
    op = InstallFileOp(
        source_path=str(source),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
    )
    manifest = Manifest(version=1, deploy_id="t", operations=(op,))

    server, client = _socket_pair()
    client.sendall(serialize_manifest(manifest))
    client.shutdown(socket.SHUT_WR)
    _serve_connection(
        server,
        expected_uid=os.getuid(),
        allowlist=_allowlist_for(src_dir, dest_dir),
    )
    response = _recv_json(client)
    assert response["status"] == "ok"


# ---------------------------------------------------------------------------
# Argv parsing for the renderer-emitted ExecStart
# ---------------------------------------------------------------------------


def test_parse_allowlist_extracts_repeated_allow_pairs(tmp_path: Path) -> None:
    a_src = tmp_path / "a_src"
    a_dst = tmp_path / "a_dst"
    b_src = tmp_path / "b_src"
    b_dst = tmp_path / "b_dst"
    argv = [
        "--allow",
        f"{a_src}:{a_dst}",
        "--allow",
        f"{b_src}:{b_dst}",
    ]
    al = _parse_allowlist(argv)
    assert al.entries[0].source_prefix == a_src
    assert al.entries[0].dest_prefix == a_dst
    assert al.entries[1].source_prefix == b_src
    assert al.entries[1].dest_prefix == b_dst


@pytest.fixture
def _silence_logger():
    yield

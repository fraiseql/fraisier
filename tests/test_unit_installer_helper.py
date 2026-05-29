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
    _execute_install_file_op,
    _handle_manifest,
    _parse_allowlist,
    _resolve_allowlist,
    _serve_connection,
)
from fraisier.unit_installer_protocol import (
    Allowlist,
    AllowlistEntry,
    DaemonReloadAction,
    InstallFileOp,
    Manifest,
    ManifestRejected,
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
    allowlist = _allowlist_for(src_dir, dest_dir)
    _handle_manifest(
        server, allowlist=allowlist, resolved=_resolve_allowlist(allowlist)
    )
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


# ---------------------------------------------------------------------------
# Cycle 4.4 — unauthorized manifest → {"status": "rejected"}, no writes
# ---------------------------------------------------------------------------


def test_handle_manifest_rejects_unauthorized_source_and_does_not_write(
    tmp_path: Path,
) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    bad_src = elsewhere / "foo.timer"
    bad_src.write_text("x")
    op = InstallFileOp(
        source_path=str(bad_src),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
    )
    manifest = Manifest(version=1, deploy_id="t", operations=(op,))

    server, client = _socket_pair()
    client.sendall(serialize_manifest(manifest))
    client.shutdown(socket.SHUT_WR)
    allowlist = _allowlist_for(src_dir, dest_dir)
    _handle_manifest(
        server, allowlist=allowlist, resolved=_resolve_allowlist(allowlist)
    )
    response = _recv_json(client)

    assert response["status"] == "rejected"
    assert "source" in response["reason"].lower()
    # The would-be-dest file must not exist (no partial write).
    assert not (dest_dir / "foo.timer").exists()


# ---------------------------------------------------------------------------
# Cycle 4.5 — TOCTOU: dest-parent symlink flipped between snapshot and execute
# ---------------------------------------------------------------------------


def test_execute_install_file_op_aborts_on_dest_parent_symlink_flip(
    tmp_path: Path,
) -> None:
    """If the dest_prefix is replaced by a symlink to outside the snapshot,
    the executor aborts before writing — even though Path.resolve() would
    naively follow the symlink to the new target."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    source = src_dir / "foo.timer"
    source.write_text("[Unit]\n")
    allowlist = _allowlist_for(src_dir, dest_dir)
    resolved = _resolve_allowlist(allowlist)  # snapshot BEFORE the flip

    # Adversarial action: replace dest_dir with a symlink to an unauthorised dir.
    outside = tmp_path / "outside"
    outside.mkdir()
    dest_dir.rmdir()
    dest_dir.symlink_to(outside)

    op = InstallFileOp(
        source_path=str(source),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
    )
    with pytest.raises(ManifestRejected, match="TOCTOU"):
        _execute_install_file_op(op, resolved=resolved)
    # Critically: no file landed at the adversary's target.
    assert not (outside / "foo.timer").exists()

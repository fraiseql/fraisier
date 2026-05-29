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
    EnableNowAction,
    InstallFileOp,
    Manifest,
    ManifestRejected,
    MarkerMeta,
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


# ---------------------------------------------------------------------------
# Cycle 4.6 — daemon_reload + enable_now post-actions
# ---------------------------------------------------------------------------


def test_handle_manifest_runs_daemon_reload_and_enable_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-actions invoke systemctl with the expected argv; result is in response."""
    import subprocess

    src_dir, dest_dir = _seed_layout(tmp_path)
    source = src_dir / "foo.timer"
    source.write_text("[Unit]\n")
    op = InstallFileOp(
        source_path=str(source),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
    )
    manifest = Manifest(
        version=1,
        deploy_id="t",
        operations=(op,),
        post_actions=(DaemonReloadAction(), EnableNowAction(unit="foo.timer")),
    )

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("fraisier.unit_installer_helper.subprocess.run", fake_run)

    server, client = _socket_pair()
    client.sendall(serialize_manifest(manifest))
    client.shutdown(socket.SHUT_WR)
    allowlist = _allowlist_for(src_dir, dest_dir)
    _handle_manifest(
        server, allowlist=allowlist, resolved=_resolve_allowlist(allowlist)
    )
    response = _recv_json(client)

    assert response["status"] == "ok"
    assert calls == [
        ["/usr/bin/systemctl", "daemon-reload"],
        ["/usr/bin/systemctl", "enable", "--now", "foo.timer"],
    ]
    kinds = [r["kind"] for r in response["post_actions"]]
    assert kinds == ["daemon-reload", "enable"]
    assert all(r["ok"] for r in response["post_actions"])


# ---------------------------------------------------------------------------
# Cycle 4.7 — install_file op with marker → sidecar 0600 file
# ---------------------------------------------------------------------------


def test_handle_manifest_busy_when_lock_held(tmp_path: Path) -> None:
    """Cycle 4.8 — concurrent manifest in flight ⇒ {"status": "busy"}."""
    import fcntl

    src_dir, dest_dir = _seed_layout(tmp_path)
    source = src_dir / "foo.timer"
    source.write_text("[Unit]\n")
    op = InstallFileOp(
        source_path=str(source),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
    )
    manifest = Manifest(version=1, deploy_id="t", operations=(op,))

    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    lock_path = lock_dir / "test.lock"

    # Hold the flock externally (simulates another helper invocation in flight).
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        server, client = _socket_pair()
        client.sendall(serialize_manifest(manifest))
        client.shutdown(socket.SHUT_WR)
        allowlist = _allowlist_for(src_dir, dest_dir)
        _handle_manifest(
            server,
            allowlist=allowlist,
            resolved=_resolve_allowlist(allowlist),
            lock_path=lock_path,
        )
        response = _recv_json(client)
        assert response["status"] == "busy"
        assert "concurrent" in response["reason"].lower()
        # Critically: no write happened.
        assert not (dest_dir / "foo.timer").exists()
    finally:
        os.close(fd)


def test_handle_manifest_timeout_when_deadline_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cycle 4.9 — wall-clock cap triggers ``{"status": "timeout"}``."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    source = src_dir / "foo.timer"
    source.write_text("[Unit]\n")
    op = InstallFileOp(
        source_path=str(source),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
    )
    manifest = Manifest(version=1, deploy_id="t", operations=(op,))

    # Force the deadline check to fire immediately.
    monkeypatch.setattr("fraisier.unit_installer_helper._MANIFEST_TIMEOUT", -1)

    server, client = _socket_pair()
    client.sendall(serialize_manifest(manifest))
    client.shutdown(socket.SHUT_WR)
    allowlist = _allowlist_for(src_dir, dest_dir)
    _handle_manifest(
        server, allowlist=allowlist, resolved=_resolve_allowlist(allowlist)
    )
    response = _recv_json(client)
    assert response["status"] == "timeout"
    assert "cap" in response["reason"].lower()


def test_handle_manifest_rejects_oversize_wire_payload(tmp_path: Path) -> None:
    """Cycle 4.10 — >1 MiB payload rejected before parse.

    The payload exceeds socketpair buffer space, so ``sendall`` would block
    until the server reads. Run the write in a thread so the server can
    drain it.
    """
    import threading

    src_dir, dest_dir = _seed_layout(tmp_path)
    server, client = _socket_pair()
    payload = b"x" * (1024 * 1024 + 10)  # no newline

    def _push() -> None:
        client.sendall(payload)
        client.shutdown(socket.SHUT_WR)

    writer = threading.Thread(target=_push, daemon=True)
    writer.start()
    allowlist = _allowlist_for(src_dir, dest_dir)
    _handle_manifest(
        server, allowlist=allowlist, resolved=_resolve_allowlist(allowlist)
    )
    writer.join(timeout=5)
    response = _recv_json(client)
    assert response["status"] == "rejected"
    assert "too large" in response["reason"].lower()


# ---------------------------------------------------------------------------
# Post-Phase-7 TOCTOU hardening — dir_fd + O_NOFOLLOW
# ---------------------------------------------------------------------------


def test_execute_refuses_symlink_at_dest_basename(tmp_path: Path) -> None:
    """If basename inside dest_prefix is a symlink to /etc/passwd (or any victim),
    the executor refuses to follow it instead of clobbering the victim.

    Pre-hardening: shutil.copy2 would have followed the symlink and written
    source bytes through it, overwriting whatever the symlink pointed at.
    Post-hardening: ``os.open(O_NOFOLLOW)`` fails with ELOOP and we abort.
    """
    src_dir, dest_dir = _seed_layout(tmp_path)
    source = src_dir / "foo.timer"
    source.write_text("[Unit]\n")

    # Pre-plant a symlink at the dest basename pointing at a victim file
    # outside the allowlist. Simulates an attacker with prior write access.
    victim = tmp_path / "victim.conf"
    victim.write_text("DO NOT OVERWRITE\n")
    (dest_dir / "foo.timer").symlink_to(victim)

    allowlist = _allowlist_for(src_dir, dest_dir)
    resolved = _resolve_allowlist(allowlist)
    op = InstallFileOp(
        source_path=str(source),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
    )
    with pytest.raises(ManifestRejected, match="symlink"):
        _execute_install_file_op(op, resolved=resolved)
    # Victim is unchanged; the symlink wasn't followed.
    assert victim.read_text() == "DO NOT OVERWRITE\n"


def test_execute_refuses_dest_parent_inode_swap(tmp_path: Path) -> None:
    """If the dest_prefix path still exists but its inode changed (someone
    unlinked the directory and re-created it), the dev/inode check catches it.

    A clean rmdir + mkdir at the same path is enough to change the inode —
    the new directory's inode number differs from the snapshot.
    """
    src_dir, dest_dir = _seed_layout(tmp_path)
    source = src_dir / "foo.timer"
    source.write_text("[Unit]\n")
    allowlist = _allowlist_for(src_dir, dest_dir)
    resolved = _resolve_allowlist(allowlist)  # snapshot has the original inode

    # Adversarial action: unlink + recreate at the same path.
    dest_dir.rmdir()
    dest_dir.mkdir()

    op = InstallFileOp(
        source_path=str(source),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
    )
    with pytest.raises(ManifestRejected, match="dev/inode"):
        _execute_install_file_op(op, resolved=resolved)
    # No file landed in the fresh directory either.
    assert not (dest_dir / "foo.timer").exists()


def test_execute_refuses_source_swapped_to_symlink_outside_snapshot(
    tmp_path: Path,
) -> None:
    """If source.resolve() at execute time lands outside the snapshot's
    source_prefixes (deploy_user swapped a symlink between validate and
    execute), the executor aborts before opening the malicious target."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    allowlist = _allowlist_for(src_dir, dest_dir)
    resolved = _resolve_allowlist(allowlist)

    # Source path is a symlink to a file outside the allowlist.
    outside = tmp_path / "outside_target"
    outside.write_text("[Service]\nExecStart=/bin/evil\n")
    sneaky_source = src_dir / "foo.timer"
    sneaky_source.symlink_to(outside)

    op = InstallFileOp(
        source_path=str(sneaky_source),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
    )
    with pytest.raises(ManifestRejected, match="source"):
        _execute_install_file_op(op, resolved=resolved)
    assert not (dest_dir / "foo.timer").exists()


def test_execute_refuses_marker_path_symlink(tmp_path: Path) -> None:
    """A pre-planted symlink at <unit>.fraisier-managed is rejected, not followed."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    source = src_dir / "foo.timer"
    source.write_text("[Unit]\n")
    victim = tmp_path / "marker_victim.conf"
    victim.write_text("MARKER VICTIM\n")
    (dest_dir / "foo.timer.fraisier-managed").symlink_to(victim)

    allowlist = _allowlist_for(src_dir, dest_dir)
    resolved = _resolve_allowlist(allowlist)
    marker = MarkerMeta(
        fraises_yaml_path="/opt/myproj/fraises.yaml",
        fraise_name="alerter",
        environment="production",
        job_name="poll",
    )
    op = InstallFileOp(
        source_path=str(source),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
        marker=marker,
    )
    with pytest.raises(ManifestRejected, match="symlink"):
        _execute_install_file_op(op, resolved=resolved)
    # Victim is unchanged.
    assert victim.read_text() == "MARKER VICTIM\n"
    # The unit file IS still written successfully before the marker step
    # fails — error-loud semantics (consistent with the rest of the helper).
    # That's documented behaviour; callers run scheduled-install to converge.


def test_install_file_op_with_marker_writes_sidecar_mode_0600(
    tmp_path: Path,
) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    source = src_dir / "foo.timer"
    source.write_text("[Unit]\n")
    marker = MarkerMeta(
        fraises_yaml_path="/opt/myproj/fraises.yaml",
        fraise_name="alerter",
        environment="production",
        job_name="poll",
    )
    op = InstallFileOp(
        source_path=str(source),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
        marker=marker,
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

    assert response["status"] == "ok"
    sidecar = dest_dir / "foo.timer.fraisier-managed"
    assert sidecar.exists()
    assert (sidecar.stat().st_mode & 0o777) == 0o600
    payload = json.loads(sidecar.read_text())
    assert payload == {
        "version": 1,
        "fraises_yaml_path": "/opt/myproj/fraises.yaml",
        "fraise_name": "alerter",
        "environment": "production",
        "job_name": "poll",
    }

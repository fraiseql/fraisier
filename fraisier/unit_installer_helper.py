"""Root-privileged unit-installer helper via Unix socket (socket-activated).

Receives a manifest of file-install operations + systemctl post-actions over
a Unix socket. Performs allowlist + symlink-escape validation, flock-protected
execution, per-op + overall timeouts, optional marker-file writes.

See ``fraisier.unit_installer_protocol`` for the wire format. SO_PEERCRED
enforcement delegates to ``fraisier._peer_creds``.

Phase 4 of bundle A (#240). Consumed by Phase 6's
``apply_unit_diffs_via_helper`` client; lifecycle managed by Phase 5's
renderer (``fraisier-<project>-<env>-unit-installer.{socket,service}``).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

from fraisier._peer_creds import check_peer_creds, extract_deploy_uid
from fraisier.unit_installer_protocol import (
    Allowlist,
    AllowlistEntry,
    DaemonReloadAction,
    DisableNowAction,
    EnableNowAction,
    InstallFileOp,
    Manifest,
    ManifestRejected,
    MarkerMeta,
    PostAction,
    StopAction,
    parse_manifest,
    render_response,
    validate_manifest,
)

_SYSTEMCTL = "/usr/bin/systemctl"
_DAEMON_RELOAD_TIMEOUT = 30
_ENABLE_NOW_TIMEOUT = 60
_DISABLE_NOW_TIMEOUT = 60
_STOP_TIMEOUT = 60

# Wall-clock cap on overall manifest execution (cycle 4.9). Checked between
# ops + post-actions. Per-op subprocess timeouts (above) bound each individual
# call; this cap defends against many-small-ops manifests running away.
_MANIFEST_TIMEOUT = 300

_DEFAULT_LOCK_DIR = Path("/var/lib/fraisier/locks")


class _ManifestTimedOut(Exception):
    """Raised when wall-clock execution exceeds ``_MANIFEST_TIMEOUT``."""


class _LockBusy(Exception):
    """Raised when the per-helper flock is held by another manifest."""


logger = logging.getLogger(__name__)

# One byte over the protocol cap so we can detect oversize.
_MAX_READ_BYTES = 1024 * 1024 + 1
_INSTALL_FILE_MODE = 0o644
_MARKER_FILE_MODE = 0o600


@dataclass(frozen=True)
class ResolvedAllowlist:
    """Frozen snapshot of allowlist prefixes resolved at helper startup.

    The execute layer compares ``Path.resolve()`` results against this
    snapshot — not against the protocol-level ``Allowlist.entries[i].
    dest_prefix.resolve()``. If an attacker flips a dest-prefix directory
    into a symlink between validate and execute, the live ``resolve``
    would follow it; the snapshot remembers where the prefix pointed at
    startup and refuses to write elsewhere (cycle 4.5).
    """

    dest_prefixes: tuple[Path, ...]
    source_prefixes: tuple[Path, ...]


def _resolve_allowlist(allowlist: Allowlist) -> ResolvedAllowlist:
    """Resolve every prefix once; snapshot defends against TOCTOU flips."""
    return ResolvedAllowlist(
        dest_prefixes=tuple(
            e.dest_prefix.resolve(strict=True) for e in allowlist.entries
        ),
        source_prefixes=tuple(
            e.source_prefix.resolve(strict=True) for e in allowlist.entries
        ),
    )


# ---------------------------------------------------------------------------
# Manifest read + dispatch
# ---------------------------------------------------------------------------


def _read_manifest_bytes(conn: socket.socket) -> bytes:
    """Drain ``conn`` up to ``_MAX_READ_BYTES`` and return the first JSON line."""
    buf = bytearray()
    while len(buf) < _MAX_READ_BYTES:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in buf:
            return bytes(buf.split(b"\n", 1)[0]) + b"\n"
    return bytes(buf)


def _handle_manifest(
    conn: socket.socket,
    *,
    allowlist: Allowlist,
    resolved: ResolvedAllowlist,
    lock_path: Path | None = None,
) -> None:
    """Read one manifest, validate, execute, write structured response.

    The whole execution runs under a non-blocking flock at ``lock_path``;
    if another manifest is in flight the response is ``{"status": "busy"}``.
    """
    raw = _read_manifest_bytes(conn)
    try:
        manifest = parse_manifest(raw)
    except ManifestRejected as exc:
        conn.sendall(render_response("rejected", reason=str(exc)))
        return
    try:
        validate_manifest(manifest, allowlist)
    except ManifestRejected as exc:
        conn.sendall(render_response("rejected", reason=str(exc)))
        return
    try:
        with _flock_or_busy(lock_path):
            try:
                response = _execute_manifest(manifest, resolved=resolved)
            except ManifestRejected as exc:
                # Mid-flight TOCTOU rejection (cycle 4.5). Any already-written
                # ops in this manifest do NOT roll back — consistent with
                # error-loud semantics shared with apply_unit_diffs.
                conn.sendall(render_response("rejected", reason=str(exc)))
                return
            except _ManifestTimedOut as exc:
                conn.sendall(render_response("timeout", reason=str(exc)))
                return
    except _LockBusy:
        conn.sendall(render_response("busy", reason="concurrent manifest in flight"))
        return
    conn.sendall(response)


@contextlib.contextmanager
def _flock_or_busy(lock_path: Path | None) -> Iterator[None]:
    """Acquire a non-blocking flock at ``lock_path``.

    ``lock_path=None`` skips locking entirely — used by tests that don't
    exercise the concurrency boundary. Production always passes a real path.
    """
    if lock_path is None:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _LockBusy from exc
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _execute_manifest(manifest: Manifest, *, resolved: ResolvedAllowlist) -> bytes:
    """Apply each op then each post-action in order. Returns a structured response.

    Wall-clock checked between each op + post-action. Exceeding
    ``_MANIFEST_TIMEOUT`` raises ``_ManifestTimedOut``; the caller (handler)
    converts it to a ``{"status": "timeout"}`` response.
    """
    start = time.monotonic()
    written: list[str] = []
    for op_index, op in enumerate(manifest.operations):
        _check_manifest_deadline(start, stage="install_file", op_index=op_index)
        _execute_install_file_op(op, resolved=resolved)
        written.append(Path(op.dest_path).name)
    post_action_results: list[dict] = []
    for action_index, action in enumerate(manifest.post_actions):
        _check_manifest_deadline(start, stage="post_action", op_index=action_index)
        post_action_results.append(_execute_post_action(action))
    return render_response("ok", installed=written, post_actions=post_action_results)


def _check_manifest_deadline(start: float, *, stage: str, op_index: int) -> None:
    if time.monotonic() - start > _MANIFEST_TIMEOUT:
        msg = (
            f"manifest exceeded {_MANIFEST_TIMEOUT}s cap at stage={stage} "
            f"op_index={op_index}"
        )
        raise _ManifestTimedOut(msg)


def _execute_post_action(action: PostAction) -> dict:
    """Run a systemctl-based post-action; return a structured per-op result.

    The helper does NOT raise on systemctl non-zero — that's a deployment
    issue surfaced in the response, not a manifest-validation issue. The
    caller (Phase 6 client) decides whether to retry or surface.
    """
    cmd, timeout = _post_action_cmd(action)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return {
            "kind": cmd[1] if len(cmd) > 1 else "unknown",
            "ok": False,
            "timeout": True,
        }
    return {
        "kind": cmd[1] if len(cmd) > 1 else "unknown",
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stderr": result.stderr,
    }


def _post_action_cmd(action: PostAction) -> tuple[list[str], int]:
    match action:
        case DaemonReloadAction():
            return ([_SYSTEMCTL, "daemon-reload"], _DAEMON_RELOAD_TIMEOUT)
        case EnableNowAction(unit=unit):
            return ([_SYSTEMCTL, "enable", "--now", unit], _ENABLE_NOW_TIMEOUT)
        case DisableNowAction(unit=unit):
            return ([_SYSTEMCTL, "disable", "--now", unit], _DISABLE_NOW_TIMEOUT)
        case StopAction(unit=unit):
            return ([_SYSTEMCTL, "stop", unit], _STOP_TIMEOUT)
    msg = f"unsupported post-action: {action!r}"
    raise TypeError(msg)


def _execute_install_file_op(
    op: InstallFileOp,
    *,
    resolved: ResolvedAllowlist,
) -> None:
    """Copy source bytes to dest, chmod 0644, write marker if present.

    Cycle 4.5 TOCTOU defense: re-resolve dest parent and compare against
    the snapshot taken at helper startup. If an attacker has flipped the
    parent into a symlink to outside the snapshot, abort before write.
    """
    dest = Path(op.dest_path)
    parent_realpath = dest.parent.resolve(strict=True)
    if parent_realpath not in resolved.dest_prefixes:
        msg = (
            f"TOCTOU detected: dest parent {parent_realpath} no longer "
            "matches a snapshot dest_prefix (parent symlink flipped?)"
        )
        raise ManifestRejected(msg)
    final_dest = parent_realpath / dest.name
    shutil.copy2(op.source_path, final_dest)
    final_dest.chmod(_INSTALL_FILE_MODE)
    if op.marker is not None:
        _write_marker(final_dest, op.marker)


def _write_marker(unit_dest: Path, marker: MarkerMeta) -> None:
    """Write the sidecar marker JSON next to ``unit_dest`` (mode 0600)."""
    marker_path = unit_dest.with_name(unit_dest.name + ".fraisier-managed")
    payload = {
        "version": 1,
        "fraises_yaml_path": marker.fraises_yaml_path,
        "fraise_name": marker.fraise_name,
        "environment": marker.environment,
        "job_name": marker.job_name,
    }
    marker_path.write_text(json.dumps(payload) + "\n")
    marker_path.chmod(_MARKER_FILE_MODE)


# ---------------------------------------------------------------------------
# Peer-creds wrapper + main entry point
# ---------------------------------------------------------------------------


def _serve_connection(
    conn: socket.socket,
    *,
    expected_uid: int | None,
    allowlist: Allowlist,
    resolved: ResolvedAllowlist | None = None,
    lock_path: Path | None = None,
) -> None:
    """SO_PEERCRED check then ``_handle_manifest``.

    Callers normally pass ``resolved`` so the snapshot is reused across many
    connections (``main`` resolves once at startup). Tests can omit it and a
    fresh snapshot is taken — they assert TOCTOU by snapshotting BEFORE the
    filesystem manipulation themselves.
    """
    if resolved is None:
        resolved = _resolve_allowlist(allowlist)
    if expected_uid is None:
        logger.warning(
            "SO_PEERCRED check disabled: --deploy-uid not provided. "
            "Re-render this helper's unit with v0.29 scaffold-install."
        )
        _handle_manifest(
            conn, allowlist=allowlist, resolved=resolved, lock_path=lock_path
        )
        return
    try:
        check_peer_creds(conn, expected_uid=expected_uid)
    except PermissionError as exc:
        logger.warning("Rejecting connection: %s", exc)
        conn.sendall(
            render_response("rejected", reason=f"peer credentials rejected: {exc}")
        )
        return
    _handle_manifest(conn, allowlist=allowlist, resolved=resolved, lock_path=lock_path)


def main() -> None:  # pragma: no cover — exercised end-to-end in Phase 8 smoke
    """Entry point for ``fraisier-unit-installer``.

    Argv shape (set by Phase 5's renderer)::

        fraisier-unit-installer --deploy-user <name> \\
            --allow <src_prefix>:<dest_prefix> [--allow ...]

    The socket file descriptor is provided by systemd via ``LISTEN_FDS``.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    deploy_uid, remaining = extract_deploy_uid(sys.argv[1:])
    project, env, remaining = _pop_project_env(remaining)
    allowlist = _parse_allowlist(remaining)
    resolved = _resolve_allowlist(allowlist)
    lock_path = _DEFAULT_LOCK_DIR / f"unit-installer-{project}-{env}.lock"
    server_sock = _build_server_socket()
    try:
        while True:
            try:
                conn, _ = server_sock.accept()
            except OSError as exc:
                logger.error("accept() failed: %s", exc)
                break
            with conn:
                try:
                    _serve_connection(
                        conn,
                        expected_uid=deploy_uid,
                        allowlist=allowlist,
                        resolved=resolved,
                        lock_path=lock_path,
                    )
                except Exception as exc:
                    logger.exception("Unhandled error in manifest handler: %s", exc)
    finally:
        server_sock.close()


def _pop_project_env(argv: list[str]) -> tuple[str, str, list[str]]:
    """Pull ``--project <name> --env <env>`` out of argv (renderer-injected)."""
    remaining = list(argv)

    def _pop(flag: str, default: str) -> str:
        if flag not in remaining:
            return default
        i = remaining.index(flag)
        if i + 1 >= len(remaining):
            return default
        value = remaining[i + 1]
        del remaining[i : i + 2]
        return value

    project = _pop("--project", "unknown")
    env = _pop("--env", "unknown")
    return project, env, remaining


def _parse_allowlist(argv: list[str]) -> Allowlist:
    """Parse ``--allow <src>:<dest>`` pairs from ``argv``."""
    entries: list[AllowlistEntry] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--allow" and i + 1 < len(argv):
            spec = argv[i + 1]
            if ":" in spec:
                src, dst = spec.split(":", 1)
                entries.append(
                    AllowlistEntry(source_prefix=Path(src), dest_prefix=Path(dst))
                )
            i += 2
            continue
        i += 1
    return Allowlist(entries=tuple(entries))


def _build_server_socket() -> socket.socket:  # pragma: no cover
    import os

    listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
    if listen_fds < 1:
        logger.error("LISTEN_FDS not set — run via systemd socket activation")
        sys.exit(1)
    server_sock = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.setblocking(True)
    logger.info("fraisier-unit-installer ready")
    return server_sock

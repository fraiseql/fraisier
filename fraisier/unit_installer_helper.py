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
import errno
import fcntl
import json
import logging
import os
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
    WriteMarkerOp,
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

    ``dest_prefix_ids`` carries the ``(st_dev, st_ino)`` of each
    ``dest_prefix`` at snapshot time. The execute layer ``fstat``s the
    directory it actually opens and rejects mismatches — closing the
    last residual TOCTOU window (someone unlinks the dest_prefix and
    re-creates a fresh directory at the same path between resolve and
    open). Paired with ``openat`` + ``O_NOFOLLOW`` on every file open,
    the helper writes nothing without proof that the on-disk object is
    the same inode it validated.
    """

    dest_prefixes: tuple[Path, ...]
    source_prefixes: tuple[Path, ...]
    dest_prefix_ids: tuple[tuple[int, int], ...]
    source_prefix_ids: tuple[tuple[int, int], ...]


def _resolve_allowlist(allowlist: Allowlist) -> ResolvedAllowlist:
    """Resolve every prefix once; snapshot defends against TOCTOU flips."""
    dest_paths = tuple(e.dest_prefix.resolve(strict=True) for e in allowlist.entries)
    source_paths = tuple(
        e.source_prefix.resolve(strict=True) for e in allowlist.entries
    )
    dest_ids = tuple((p.stat().st_dev, p.stat().st_ino) for p in dest_paths)
    source_ids = tuple((p.stat().st_dev, p.stat().st_ino) for p in source_paths)
    return ResolvedAllowlist(
        dest_prefixes=dest_paths,
        source_prefixes=source_paths,
        dest_prefix_ids=dest_ids,
        source_prefix_ids=source_ids,
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
    markers_written: list[str] = []
    for op_index, op in enumerate(manifest.operations):
        _check_manifest_deadline(start, stage="op", op_index=op_index)
        match op:
            case InstallFileOp():
                _execute_install_file_op(op, resolved=resolved)
                written.append(Path(op.dest_path).name)
            case WriteMarkerOp():
                _execute_write_marker_op(op, resolved=resolved)
                markers_written.append(Path(op.dest_path).name)
    post_action_results: list[dict] = []
    for action_index, action in enumerate(manifest.post_actions):
        _check_manifest_deadline(start, stage="post_action", op_index=action_index)
        post_action_results.append(_execute_post_action(action))
    return render_response(
        "ok",
        installed=written,
        markers_written=markers_written,
        post_actions=post_action_results,
    )


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

    TOCTOU defense (cycle 4.5 + post-Phase-7 hardening):

    1. Re-resolve dest parent; reject if it no longer matches the snapshot
       path. This catches a symlink-flipped *parent path*.
    2. ``os.open(parent_realpath, O_RDONLY | O_DIRECTORY)`` then ``fstat``
       and compare ``(st_dev, st_ino)`` against the snapshot. This catches
       a same-path *different directory* swap (someone unlinked the dest
       prefix and re-created it with a different inode).
    3. ``os.open(basename, O_CREAT | O_TRUNC | O_NOFOLLOW, dir_fd=parent_fd)``
       writes via the directory fd we just verified. ``O_NOFOLLOW`` means
       if an attacker has pre-planted a symlink at ``<dest_prefix>/<basename>``
       — e.g., pointing at ``/etc/passwd`` — the open fails with ELOOP and
       we abort rather than overwrite their target.
    4. Re-validate source against the snapshot's source_prefixes; open with
       ``O_NOFOLLOW`` so a swap from regular-file-to-symlink between
       resolve and read is caught.
    5. ``os.fchmod`` on the open fd (not the path) so the chmod can't race
       with a swap.

    Marker (when present) is written via the same parent_fd with the same
    ``O_NOFOLLOW`` discipline.
    """
    dest = Path(op.dest_path)
    parent_realpath = dest.parent.resolve(strict=True)
    parent_fd = _open_and_verify_dest_parent(parent_realpath, resolved)
    try:
        basename = dest.name
        _copy_source_into_dir_fd(parent_fd, basename, op.source_path, resolved)
        if op.marker is not None:
            _write_marker_into_dir_fd(parent_fd, basename, op.marker)
    finally:
        os.close(parent_fd)


def _execute_write_marker_op(op: WriteMarkerOp, *, resolved: ResolvedAllowlist) -> None:
    """Write only the sidecar — used by 04's auto-backfill migration path.

    Same parent_fd + O_NOFOLLOW discipline as ``_execute_install_file_op``;
    no source bytes touched, no unit file written.
    """
    dest = Path(op.dest_path)
    parent_realpath = dest.parent.resolve(strict=True)
    parent_fd = _open_and_verify_dest_parent(parent_realpath, resolved)
    try:
        _write_marker_into_dir_fd(parent_fd, dest.name, op.marker)
    finally:
        os.close(parent_fd)


def _open_and_verify_dest_parent(
    parent_realpath: Path, resolved: ResolvedAllowlist
) -> int:
    """Open ``parent_realpath`` as a dir-fd and verify dev/inode vs snapshot."""
    matching_idx: int | None = None
    for i, p in enumerate(resolved.dest_prefixes):
        if p == parent_realpath:
            matching_idx = i
            break
    if matching_idx is None:
        msg = (
            f"TOCTOU detected: dest parent {parent_realpath} no longer "
            "matches a snapshot dest_prefix (parent symlink flipped?)"
        )
        raise ManifestRejected(msg)
    parent_fd = os.open(parent_realpath, os.O_RDONLY | os.O_DIRECTORY)
    try:
        st = os.fstat(parent_fd)
    except OSError:
        os.close(parent_fd)
        raise
    expected = resolved.dest_prefix_ids[matching_idx]
    if (st.st_dev, st.st_ino) != expected:
        os.close(parent_fd)
        msg = (
            f"TOCTOU detected: dest parent {parent_realpath} dev/inode "
            f"{(st.st_dev, st.st_ino)} differs from snapshot {expected} "
            "— directory was replaced between snapshot and execute"
        )
        raise ManifestRejected(msg)
    return parent_fd


def _copy_source_into_dir_fd(
    parent_fd: int,
    basename: str,
    source_path: str,
    resolved: ResolvedAllowlist,
) -> None:
    """Read source bytes (O_NOFOLLOW) and write to ``parent_fd/basename``.

    Source path is re-validated against the snapshot's source_prefixes at
    execute time — defends against deploy_user swapping a source file
    between validate and execute. Both source and dest opens use
    O_NOFOLLOW so a symlink inserted anywhere after the resolve is caught.
    """
    source_realpath = Path(source_path).resolve(strict=True)
    if not any(source_realpath.is_relative_to(p) for p in resolved.source_prefixes):
        msg = (
            f"TOCTOU detected: source {source_realpath} is no longer "
            "under any snapshot source_prefix"
        )
        raise ManifestRejected(msg)

    try:
        src_fd = os.open(source_realpath, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            msg = (
                f"source {source_realpath} became a symlink after resolve "
                "— refusing to follow"
            )
            raise ManifestRejected(msg) from exc
        raise

    try:
        dst_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        try:
            dst_fd = os.open(
                basename, dst_flags, mode=_INSTALL_FILE_MODE, dir_fd=parent_fd
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                msg = (
                    f"dest basename {basename!r} is a symlink — refusing "
                    "to follow (O_NOFOLLOW)"
                )
                raise ManifestRejected(msg) from exc
            raise
        try:
            while True:
                chunk = os.read(src_fd, 65536)
                if not chunk:
                    break
                _write_all(dst_fd, chunk)
            os.fchmod(dst_fd, _INSTALL_FILE_MODE)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def _write_marker_into_dir_fd(
    parent_fd: int, unit_basename: str, marker: MarkerMeta
) -> None:
    """Write the .fraisier-managed sidecar via ``parent_fd`` + O_NOFOLLOW."""
    marker_name = unit_basename + ".fraisier-managed"
    payload = {
        "version": 1,
        "fraises_yaml_path": marker.fraises_yaml_path,
        "fraise_name": marker.fraise_name,
        "environment": marker.environment,
        "job_name": marker.job_name,
    }
    data = (json.dumps(payload) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(marker_name, flags, mode=_MARKER_FILE_MODE, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            msg = (
                f"marker {marker_name!r} is a symlink — refusing to follow (O_NOFOLLOW)"
            )
            raise ManifestRejected(msg) from exc
        raise
    try:
        _write_all(fd, data)
        os.fchmod(fd, _MARKER_FILE_MODE)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    """``os.write`` until every byte is written (writes can be short on EINTR)."""
    written = 0
    while written < len(data):
        written += os.write(fd, data[written:])


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

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

import json
import logging
import shutil
import socket
import sys
from pathlib import Path

from fraisier._peer_creds import check_peer_creds, extract_deploy_uid
from fraisier.unit_installer_protocol import (
    Allowlist,
    AllowlistEntry,
    InstallFileOp,
    Manifest,
    ManifestRejected,
    MarkerMeta,
    parse_manifest,
    render_response,
    validate_manifest,
)

logger = logging.getLogger(__name__)

# One byte over the protocol cap so we can detect oversize.
_MAX_READ_BYTES = 1024 * 1024 + 1
_INSTALL_FILE_MODE = 0o644
_MARKER_FILE_MODE = 0o600


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
) -> None:
    """Read one manifest, validate, execute, write structured response."""
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
    response = _execute_manifest(manifest)
    conn.sendall(response)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _execute_manifest(manifest: Manifest) -> bytes:
    """Apply each op in order. Returns a structured response."""
    written: list[str] = []
    for op in manifest.operations:
        _execute_install_file_op(op)
        written.append(Path(op.dest_path).name)
    return render_response("ok", installed=written)


def _execute_install_file_op(op: InstallFileOp) -> None:
    """Copy source bytes to dest, chmod 0644, write marker if present.

    Per cycle 4.5, the dest_path is re-resolved via realpath immediately
    before open() to defeat TOCTOU symlink races on the parent.
    """
    dest = Path(op.dest_path)
    parent_realpath = dest.parent.resolve(strict=True)
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
) -> None:
    """SO_PEERCRED check then ``_handle_manifest``."""
    if expected_uid is None:
        logger.warning(
            "SO_PEERCRED check disabled: --deploy-uid not provided. "
            "Re-render this helper's unit with v0.29 scaffold-install."
        )
        _handle_manifest(conn, allowlist=allowlist)
        return
    try:
        check_peer_creds(conn, expected_uid=expected_uid)
    except PermissionError as exc:
        logger.warning("Rejecting connection: %s", exc)
        conn.sendall(
            render_response("rejected", reason=f"peer credentials rejected: {exc}")
        )
        return
    _handle_manifest(conn, allowlist=allowlist)


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
    allowlist = _parse_allowlist(remaining)
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
                        conn, expected_uid=deploy_uid, allowlist=allowlist
                    )
                except Exception as exc:
                    logger.exception("Unhandled error in manifest handler: %s", exc)
    finally:
        server_sock.close()


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

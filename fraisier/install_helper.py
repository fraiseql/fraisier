"""Install helper via Unix socket (systemd socket-activated).

Receives JSON commands from the fraisier webhook/deployer over a Unix socket
and executes the install command as the configured install user. This removes
the need for sudo in the deploy/webhook services and allows
NoNewPrivileges=true on all services.

The service unit runs as install_user (set via User= in systemd), so no
privilege escalation is needed — the socket is the security boundary.

Protocol
--------
Request (one JSON line + newline)::

    {"command": ["uv", "sync", "--frozen"], "cwd": "/var/www/app"}

Response (one JSON line + newline)::

    {"ok": true, "stdout": "...", "stderr": "...", "returncode": 0}

Error response::

    {"ok": false, "error": "invalid request: ..."}
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys

from fraisier._peer_creds import check_peer_creds, extract_deploy_uid

logger = logging.getLogger(__name__)

_peer_creds_skip_warned = False


def _send_error(conn: socket.socket, message: str) -> None:
    """Send an error response to *conn*."""
    _send_response(conn, {"ok": False, "error": message})


def _send_response(conn: socket.socket, response: dict) -> None:
    """Serialise *response* as a JSON line and send it on *conn*."""
    try:
        conn.sendall(json.dumps(response).encode() + b"\n")
    except OSError as exc:
        logger.warning("Failed to send response: %s", exc)


def _handle_connection(conn: socket.socket, allowed_command: list[str]) -> None:
    """Read one JSON request from *conn*, execute it, send JSON response.

    Args:
        conn: Connected socket for this request.
        allowed_command: The exact command list this helper is authorised to run.
            Baked in at service-unit render time — acts as a security allowlist.
    """
    with conn:
        raw = b""
        try:
            buf = bytearray()
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
                if b"\n" in buf:
                    raw = bytes(buf.split(b"\n", 1)[0]) + b"\n"
                    break
        except OSError as exc:
            logger.warning("Read error: %s", exc)
            return

        if not raw.strip():
            return

        try:
            request = json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Malformed request: %s", exc)
            _send_error(conn, f"malformed JSON: {exc}")
            return

        command = request.get("command")
        cwd = request.get("cwd")

        if not isinstance(command, list) or not command:
            _send_error(conn, "invalid request: 'command' must be a non-empty list")
            return
        if not all(isinstance(s, str) for s in command):
            _send_error(conn, "invalid request: 'command' elements must be strings")
            return
        if not isinstance(cwd, str) or not cwd.startswith("/"):
            _send_error(conn, "invalid request: 'cwd' must be an absolute path")
            return

        if command != allowed_command:
            logger.warning("Command not allowed: %s", command)
            _send_error(conn, "command not allowed")
            return

        logger.info("Running install command: %s in %s", command, cwd)

        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.error("Install command timed out: %s", command)
            _send_error(conn, "install command timed out")
            return
        except OSError as exc:
            logger.error("Failed to run install command: %s", exc)
            _send_error(conn, f"failed to run command: {exc}")
            return

        response = {
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
        if result.returncode != 0:
            logger.error(
                "Install command %s exited %d: %s",
                command,
                result.returncode,
                result.stderr.strip(),
            )
            if "__pycache__" in result.stderr and "Permission denied" in result.stderr:
                response["advice"] = (
                    "Root-owned __pycache__ directories are blocking uv sync. "
                    f"Fix: sudo find {cwd}/.venv -name __pycache__ -user root "
                    "-type d -exec rm -rf {{}} + then retry the deployment. "
                    "The venv may be corrupted — run uv sync --frozen manually "
                    "after cleanup. See: https://github.com/fraiseql/fraisier/issues/196"
                )

        _send_response(conn, response)


def _serve_connection(
    conn: socket.socket,
    *,
    expected_uid: int | None,
    allowed_command: list[str],
) -> None:
    """Enforce SO_PEERCRED then dispatch one request to ``_handle_connection``.

    ``expected_uid=None`` is the v0.29 transitional fallback — log a one-time
    warning and process the request anyway (becomes mandatory in v0.30).
    """
    global _peer_creds_skip_warned
    if expected_uid is None:
        if not _peer_creds_skip_warned:
            logger.warning(
                "SO_PEERCRED check disabled: --deploy-uid not provided. "
                "Re-render this helper's unit with v0.29 scaffold-install "
                "to close the trust gap (will become mandatory in v0.30)."
            )
            _peer_creds_skip_warned = True
        _handle_connection(conn, allowed_command=allowed_command)
        return
    try:
        check_peer_creds(conn, expected_uid=expected_uid)
    except PermissionError as exc:
        logger.warning("Rejecting connection: %s", exc)
        with conn:
            _send_error(conn, f"peer credentials rejected: {exc}")
        return
    _handle_connection(conn, allowed_command=allowed_command)


def main() -> None:
    """Entry point for fraisier-install-helper.

    The socket file descriptor is provided by systemd via ``LISTEN_FDS``
    (fd 3 = first socket, ``Accept=no``).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    deploy_uid, allowed_command = extract_deploy_uid(sys.argv[1:])
    if not allowed_command:
        logger.error(
            "Usage: fraisier-install-helper <command> [args...]\n"
            "  Example: fraisier-install-helper uv sync --frozen\n"
            "The allowed command is baked into the unit at scaffold render time."
        )
        sys.exit(1)

    listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
    if listen_fds < 1:
        logger.error(
            "LISTEN_FDS not set or zero — must be run via systemd socket activation.\n"
            "Check: systemctl status fraisier-*-install.socket\n"
            "Enable: systemctl enable --now fraisier-<fraise>-<env>-install.socket"
        )
        sys.exit(1)

    server_sock = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.setblocking(True)

    logger.info("fraisier-install-helper ready, allowed command: %s", allowed_command)

    try:
        while True:
            try:
                conn, _ = server_sock.accept()
            except OSError as exc:
                logger.error("accept() failed: %s", exc)
                break
            try:
                _serve_connection(
                    conn,
                    expected_uid=deploy_uid,
                    allowed_command=allowed_command,
                )
            except Exception as exc:
                # Bare-except is intentional here: any handler crash must
                # not bring down the systemd-supervised socket server.
                # logger.exception captures the traceback; binding `exc`
                # surfaces the type/repr in the rendered log line.
                logger.exception("Unhandled error in connection handler: %s", exc)
    finally:
        server_sock.close()

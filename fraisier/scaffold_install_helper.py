"""Root-privileged scaffold-install helper via Unix socket (socket-activated).

Receives {"action": "install"} from the webhook service over a Unix socket and
executes the project's install.sh as root. This removes the need for sudo in the
webhook service and is compatible with NoNewPrivileges=true.

Protocol
--------
Request (one JSON line + newline)::

    {"action": "install"}

Response (one JSON line + newline)::

    {"ok": true, "stdout": "...", "stderr": "...", "returncode": 0}

Error response::

    {"ok": false, "error": "..."}
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_ALLOWED_ACTIONS: frozenset[str] = frozenset({"install"})
_BASH = "/usr/bin/bash"


def _send_error(conn: socket.socket, message: str) -> None:
    """Send an error response to *conn*."""
    _send_response(conn, {"ok": False, "error": message})


def _send_response(conn: socket.socket, response: dict) -> None:
    """Serialise *response* as a JSON line and send it on *conn*."""
    try:
        conn.sendall(json.dumps(response).encode() + b"\n")
    except OSError as exc:
        logger.warning("Failed to send response: %s", exc)


def _handle_connection(conn: socket.socket, allowed_script: str) -> None:
    """Read one JSON request from *conn*, validate, execute install.sh, send response.

    Args:
        conn: Connected socket for this request.
        allowed_script: Absolute path to the install.sh this helper may run.
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

        action = request.get("action", "")

        if action not in _ALLOWED_ACTIONS:
            _send_error(conn, f"action not allowed: {action!r}")
            return

        if not Path(allowed_script).exists():
            _send_error(conn, f"install script not found: {allowed_script}")
            return

        try:
            result = subprocess.run(
                [_BASH, allowed_script],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.error("install.sh timed out: %s", allowed_script)
            _send_error(conn, "install.sh timed out")
            return
        except OSError as exc:
            logger.error("Failed to run install.sh: %s", exc)
            _send_error(conn, f"failed to run install.sh: {exc}")
            return

        response = {
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
        if result.returncode != 0:
            logger.error(
                "install.sh exited %d: %s",
                result.returncode,
                result.stderr.strip(),
            )

        _send_response(conn, response)


def _build_server_socket(allowed_script: str) -> socket.socket:
    """Acquire socket from systemd socket activation (LISTEN_FDS protocol).

    Args:
        allowed_script: Absolute path to the install.sh this helper may run
            (used for logging only at this stage).

    Returns:
        Server socket ready to accept connections.

    Raises:
        SystemExit: If LISTEN_FDS is not set or zero.
    """
    listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
    if listen_fds < 1:
        logger.error(
            "LISTEN_FDS not set or zero — must be run via systemd socket activation"
        )
        sys.exit(1)

    # First activated socket is fd 3 (SD_LISTEN_FDS_START = 3)
    server_sock = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.setblocking(True)

    logger.info(
        "fraisier-scaffold-install-helper ready, allowed script: %s",
        allowed_script,
    )

    return server_sock


def main() -> None:
    """Entry point for fraisier-scaffold-install-helper.

    The absolute path to the project's install.sh is passed as the first
    positional argument (baked in at template render time)::

        fraisier-scaffold-install-helper /opt/myproject/scripts/generated/install.sh

    The socket file descriptor is provided by systemd via ``LISTEN_FDS``
    (fd 3 = first socket, ``Accept=no``).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if len(sys.argv) < 2:
        logger.error(
            "Usage: fraisier-scaffold-install-helper <path-to-install.sh>"
        )
        sys.exit(1)

    allowed_script = sys.argv[1]

    if not Path(allowed_script).exists():
        logger.critical("install.sh not found at startup: %s", allowed_script)
        sys.exit(1)

    server_sock = _build_server_socket(allowed_script)

    try:
        while True:
            try:
                conn, _ = server_sock.accept()
            except OSError as exc:
                logger.error("accept() failed: %s", exc)
                break
            try:
                _handle_connection(conn, allowed_script=allowed_script)
            except Exception as exc:
                logger.exception("Unhandled error in connection handler: %s", exc)
    finally:
        server_sock.close()

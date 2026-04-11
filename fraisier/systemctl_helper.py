"""Root-privileged systemctl helper via Unix socket (systemd socket-activated).

Receives JSON commands from the fraisier webhook/deployer over a Unix socket,
validates them against an allowlist, and executes /usr/bin/systemctl directly
as root. This removes the need for sudo in the deploy/webhook services and
allows NoNewPrivileges=true on all services.

Protocol
--------
Request (one JSON line + newline)::

    {"action": "stop", "service": "api.printoptim.dev.service"}

``daemon-reload`` requests omit the ``service`` field::

    {"action": "daemon-reload"}

Response (one JSON line + newline)::

    {"ok": true, "stdout": "", "stderr": "", "returncode": 0}

Error response::

    {"ok": false, "error": "service not allowed: foo.service"}
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys

logger = logging.getLogger(__name__)

_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {"stop", "start", "restart", "is-active", "daemon-reload"}
)

_SYSTEMCTL = "/usr/bin/systemctl"


def _send_error(conn: socket.socket, message: str) -> None:
    """Send an error response to *conn*."""
    _send_response(conn, {"ok": False, "error": message})


def _send_response(conn: socket.socket, response: dict) -> None:
    """Serialise *response* as a JSON line and send it on *conn*."""
    try:
        conn.sendall(json.dumps(response).encode() + b"\n")
    except OSError as exc:
        logger.warning("Failed to send response: %s", exc)


def _handle_connection(conn: socket.socket, allowed_services: frozenset[str]) -> None:
    """Read one JSON request from *conn*, execute it, send JSON response.

    The connection is kept open for the response — we only use makefile for
    reading, and close it before sending the response.
    """
    with conn:
        # Read the request line without closing conn
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
        service = request.get("service", "")

        if action not in _ALLOWED_ACTIONS:
            _send_error(conn, f"action not allowed: {action!r}")
            return

        if action == "daemon-reload":
            cmd = [_SYSTEMCTL, "daemon-reload"]
        else:
            if not service:
                _send_error(conn, "missing 'service' field")
                return
            if service not in allowed_services:
                _send_error(conn, f"service not allowed: {service}")
                return
            cmd = [_SYSTEMCTL, action, service]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.error("systemctl timed out: %s", cmd)
            _send_error(conn, "systemctl timed out")
            return
        except OSError as exc:
            logger.error("Failed to run systemctl: %s", exc)
            _send_error(conn, f"failed to run systemctl: {exc}")
            return

        response = {
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
        if result.returncode != 0:
            logger.error(
                "systemctl %s %s exited %d: %s",
                action,
                service,
                result.returncode,
                result.stderr.strip(),
            )

        _send_response(conn, response)


def _build_server_socket(allowed_services: frozenset[str]) -> socket.socket:
    """Acquire socket from systemd socket activation (LISTEN_FDS protocol).

    Args:
        allowed_services: Set of allowed service names (used for logging).

    Returns:
        Server socket ready to accept connections.

    Raises:
        SystemExit: If LISTEN_FDS is not set or zero.
    """
    # Acquire socket from systemd socket activation (LISTEN_FDS protocol)
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
        "fraisier-systemctl-helper ready, allowed services: %s",
        sorted(allowed_services),
    )

    return server_sock


def main() -> None:
    """Entry point for fraisier-systemctl-helper.

    Allowed services are passed as positional arguments::

        fraisier-systemctl-helper api.printoptim.dev.service api.printoptim.st.service

    The socket file descriptor is provided by systemd via ``LISTEN_FDS``
    (fd 3 = first socket, ``Accept=no``).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    allowed_services: frozenset[str] = frozenset(sys.argv[1:])

    if not allowed_services:
        logger.warning(
            "No allowed services specified — all service calls will be denied"
        )

    server_sock = _build_server_socket(allowed_services)

    try:
        while True:
            try:
                conn, _ = server_sock.accept()
            except OSError as exc:
                logger.error("accept() failed: %s", exc)
                break
            try:
                _handle_connection(conn, allowed_services)
            except Exception as exc:
                # Bare-except is intentional here: any handler crash must
                # not bring down the systemd-supervised socket server.
                # logger.exception captures the traceback; binding `exc`
                # surfaces the type/repr in the rendered log line.
                logger.exception("Unhandled error in connection handler: %s", exc)
    finally:
        server_sock.close()

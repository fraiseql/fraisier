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

from fraisier._peer_creds import check_peer_creds, extract_deploy_uid

logger = logging.getLogger(__name__)

_ALLOWED_ACTIONS: frozenset[str] = frozenset({"install"})
_BASH = "/usr/bin/bash"

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

        # Tell install.sh it is being executed BY this helper so it skips
        # restarting THIS helper's own socket. `systemctl restart
        # …scaffold-install-helper.socket` would SIGTERM this very process
        # mid-request; the client would then read an empty reply and — under the
        # webhook's NoNewPrivileges (which cannot fall back) — the deploy aborts
        # before the DB step. See install.sh.j2's scaffold-install-helper block.
        helper_env = {**os.environ, "FRAISIER_VIA_SCAFFOLD_INSTALL_HELPER": "1"}

        # Forward the client's declaration that a deploy is in flight, so
        # install.sh does not restart the unit that deploy is running inside
        # (#349). This travels in the request because the helper is a separate
        # root service and does not inherit the deploy's environment.
        #
        # Only a literal JSON `true` is honoured, and it is mapped to a fixed
        # value rather than interpolated: the payload reaches a daemon running
        # as root, so a client must not be able to name an environment variable
        # or choose its contents.
        if request.get("deploy_in_flight") is True:
            helper_env["FRAISIER_DEPLOY_IN_FLIGHT"] = "1"
        try:
            result = subprocess.run(
                [_BASH, allowed_script],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
                env=helper_env,
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


def _serve_connection(
    conn: socket.socket,
    *,
    expected_uid: int | None,
    allowed_script: str,
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
        _handle_connection(conn, allowed_script=allowed_script)
        return
    try:
        check_peer_creds(conn, expected_uid=expected_uid)
    except PermissionError as exc:
        logger.warning("Rejecting connection: %s", exc)
        with conn:
            _send_error(conn, f"peer credentials rejected: {exc}")
        return
    _handle_connection(conn, allowed_script=allowed_script)


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

    deploy_uid, remaining = extract_deploy_uid(sys.argv[1:])

    if not remaining:
        logger.error("Usage: fraisier-scaffold-install-helper <path-to-install.sh>")
        sys.exit(1)

    allowed_script = remaining[0]

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
                _serve_connection(
                    conn,
                    expected_uid=deploy_uid,
                    allowed_script=allowed_script,
                )
            except Exception as exc:
                logger.exception("Unhandled error in connection handler: %s", exc)
    finally:
        server_sock.close()

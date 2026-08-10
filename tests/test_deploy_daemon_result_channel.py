"""The daemon's JSON result reaches the client that asked for it (#356).

``deploy-service.j2`` sets ``StandardInput=socket`` under a socket unit with
``Accept=yes``, so fd 0 *is* the accepted connection. It also sets
``StandardOutput=journal``, so the ``print()`` that carried the machine-readable
result sent it to the journal instead — never to the client. Every ``--wait``
run in this project's history therefore read an empty response.

These tests drive a real ``socketpair`` on fd 0 rather than mocking the write:
what the daemon puts on the wire is the whole subject.
"""

from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from fraisier.cli.main import main

if TYPE_CHECKING:
    from collections.abc import Iterator

REQUEST = json.dumps(
    {
        "version": 1,
        "project": "api",
        "environment": "staging",
        "branch": "main",
        "timestamp": "2026-08-09T02:00:00Z",
        "triggered_by": "cli",
        "options": {"force": False, "no_cache": False, "dry_run": False},
        "metadata": {"cli_user": "deploy"},
    }
)


@contextmanager
def accepted_connection() -> Iterator[socket.socket]:
    """Put a real AF_UNIX stream socket on fd 0, as systemd's Accept=yes does.

    Yields the client end — the side ``trigger-deploy`` holds. fd 0 is restored
    unconditionally on the way out, so a failing test cannot poison the ones
    after it.
    """
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    saved_stdin = os.dup(0)
    try:
        os.dup2(server.fileno(), 0)
        yield client
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        server.close()
        client.close()


def read_result(client: socket.socket) -> bytes:
    """Read what the daemon wrote, without waiting for a close it may not do.

    The daemon writes synchronously before exiting, so by the time
    ``CliRunner.invoke`` has returned the bytes are already in the buffer. A
    short timeout keeps an empty channel from hanging the suite for minutes.
    """
    client.settimeout(2)
    try:
        return client.recv(65536)
    except TimeoutError:
        return b""


def deployment_result(**overrides: object) -> MagicMock:
    """A DeploymentResult stand-in whose attributes are JSON-serialisable."""
    result = MagicMock()
    result.success = True
    result.status = "success"
    result.message = "Deployment completed"
    result.deployed_version = "abc123"
    result.duration_seconds = 45.5
    result.error_message = None
    for name, value in overrides.items():
        setattr(result, name, value)
    return result


class TestOutcomeReachesTheConnection:
    """The two terminating paths at the end of deploy_daemon."""

    def test_successful_deployment_writes_its_result_to_the_socket(self) -> None:
        with accepted_connection() as client:
            with patch("fraisier.daemon.execute_deployment_request") as execute:
                execute.return_value = deployment_result()
                run = CliRunner().invoke(
                    main, ["deploy-daemon", "--project", "api"], input=REQUEST
                )
            wire = read_result(client)

        assert run.exit_code == 0
        payload = json.loads(wire.decode("utf-8"))
        assert payload["success"] is True
        assert payload["status"] == "success"
        assert payload["version"] == "abc123"
        assert payload["duration"] == 45.5

    def test_failed_deployment_writes_its_result_to_the_socket(self) -> None:
        with accepted_connection() as client:
            with patch("fraisier.daemon.execute_deployment_request") as execute:
                execute.return_value = deployment_result(
                    success=False,
                    status="failed",
                    message=None,
                    deployed_version=None,
                    error_message="migration failed",
                )
                run = CliRunner().invoke(
                    main, ["deploy-daemon", "--project", "api"], input=REQUEST
                )
            wire = read_result(client)

        assert run.exit_code == 1
        payload = json.loads(wire.decode("utf-8"))
        assert payload["success"] is False
        assert payload["error"] == "migration failed"

    def test_exactly_one_json_object_is_written(self) -> None:
        """One result per connection — a second would break the client's parse.

        The client reads the whole stream and hands it to ``json.loads``, which
        rejects two concatenated objects. ``raw_decode`` is what distinguishes
        "one object" from "one object followed by more".
        """
        with accepted_connection() as client:
            with patch("fraisier.daemon.execute_deployment_request") as execute:
                execute.return_value = deployment_result()
                CliRunner().invoke(
                    main, ["deploy-daemon", "--project", "api"], input=REQUEST
                )
            wire = read_result(client)

        text = wire.decode("utf-8")
        _, consumed = json.JSONDecoder().raw_decode(text)
        assert text[consumed:].strip() == "", f"trailing bytes on the wire: {text!r}"

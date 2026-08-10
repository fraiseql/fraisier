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


class TestEveryErrorPathReportsItsReason:
    """The five paths that terminate before a deployment result exists.

    Each used to print prose to the journal and exit 1 having written nothing
    machine-readable at all. #356 attributed the empty response to #349 killing
    the unit; #349 was one trigger out of six, and these five are ordinary
    control flow rather than races.
    """

    def test_empty_stdin_says_so_on_the_wire(self) -> None:
        with accepted_connection() as client:
            run = CliRunner().invoke(
                main, ["deploy-daemon", "--project", "api"], input=""
            )
            wire = read_result(client)

        assert run.exit_code == 1
        payload = json.loads(wire.decode("utf-8"))
        assert payload["success"] is False
        assert payload["error"] == "No input received on stdin"

    def test_undecodable_stdin_says_so_on_the_wire(self) -> None:
        """A client writing non-UTF-8 makes ``sys.stdin.read()`` itself raise."""
        with accepted_connection() as client:
            run = CliRunner().invoke(
                main, ["deploy-daemon", "--project", "api"], input=b"\xff\xfe\x00binary"
            )
            wire = read_result(client)

        assert run.exit_code == 1
        payload = json.loads(wire.decode("utf-8"))
        assert payload["success"] is False
        assert payload["error"].startswith("Error reading stdin:")

    def test_unparseable_request_says_so_on_the_wire(self) -> None:
        with accepted_connection() as client:
            run = CliRunner().invoke(
                main, ["deploy-daemon", "--project", "api"], input="not-valid-json"
            )
            wire = read_result(client)

        assert run.exit_code == 1
        payload = json.loads(wire.decode("utf-8"))
        assert payload["success"] is False
        assert payload["error"].startswith("Error parsing request:")

    def test_project_mismatch_says_which_projects_on_the_wire(self) -> None:
        mismatched = json.loads(REQUEST)
        mismatched["project"] = "other"

        with accepted_connection() as client:
            run = CliRunner().invoke(
                main,
                ["deploy-daemon", "--project", "api"],
                input=json.dumps(mismatched),
            )
            wire = read_result(client)

        assert run.exit_code == 1
        payload = json.loads(wire.decode("utf-8"))
        assert payload["success"] is False
        assert "requested 'other'" in payload["error"]
        assert "configured for 'api'" in payload["error"]

    def test_execution_raising_says_so_on_the_wire(self) -> None:
        with accepted_connection() as client:
            with patch("fraisier.daemon.execute_deployment_request") as execute:
                execute.side_effect = RuntimeError("git fetch: host key mismatch")
                run = CliRunner().invoke(
                    main, ["deploy-daemon", "--project", "api"], input=REQUEST
                )
            wire = read_result(client)

        assert run.exit_code == 1
        payload = json.loads(wire.decode("utf-8"))
        assert payload["success"] is False
        assert "git fetch: host key mismatch" in payload["error"]


class TestADepartedPeerCannotChangeTheOutcome:
    """`deploy-checker.service.j2:24` runs trigger-deploy with no ``--wait``.

    That client sends, shuts down its write half, skips the recv loop and
    closes — in milliseconds. Half an hour later the daemon writes its result
    to a socket whose peer is long gone. Unhandled, the `BrokenPipeError` would
    raise *after* a successful deployment and take the unit's exit code with
    it, inverting the very bug this phase fixes.

    Each test asserts the note, which is only printed from the `except OSError`
    branch — so a guard that stopped being exercised fails the test rather than
    passing it quietly.
    """

    def test_success_survives_a_peer_that_already_left(self) -> None:
        with accepted_connection() as client:
            client.close()  # what every deploy-checker fire does
            with patch("fraisier.daemon.execute_deployment_request") as execute:
                execute.return_value = deployment_result()
                run = CliRunner().invoke(
                    main, ["deploy-daemon", "--project", "api"], input=REQUEST
                )

        assert run.exit_code == 0, run.output
        assert "no client received the result" in run.output
        assert run.exception is None or isinstance(run.exception, SystemExit)

    def test_failure_still_exits_one_when_the_peer_already_left(self) -> None:
        """The exit code is the deployment's, in both directions."""
        with accepted_connection() as client:
            client.close()
            with patch("fraisier.daemon.execute_deployment_request") as execute:
                execute.return_value = deployment_result(
                    success=False, status="failed", error_message="migration failed"
                )
                run = CliRunner().invoke(
                    main, ["deploy-daemon", "--project", "api"], input=REQUEST
                )

        assert run.exit_code == 1
        assert "no client received the result" in run.output

    def test_a_closed_fd_zero_is_not_an_error(self) -> None:
        """`os.fstat(0)` raises EBADF rather than reporting "not a socket"."""
        saved_stdin = os.dup(0)
        try:
            os.close(0)
            with patch("fraisier.daemon.execute_deployment_request") as execute:
                execute.return_value = deployment_result()
                run = CliRunner().invoke(
                    main, ["deploy-daemon", "--project", "api"], input=REQUEST
                )
        finally:
            os.dup2(saved_stdin, 0)
            os.close(saved_stdin)

        assert run.exit_code == 0, run.output


class TestTheWriteDoesNotStealFdZero:
    """`socket.socket(fileno=0)` *owns* fd 0 and closes it when collected.

    Built early and dropped, that closes stdin out from under a running
    deployment. `os.dup(0)` gives the socket object an fd of its own to close.
    """

    def test_fd_zero_is_still_open_after_the_result_is_written(self) -> None:
        import gc

        from fraisier.cli._deploy import _write_to_accepted_connection

        with accepted_connection() as client:
            _write_to_accepted_connection('{"success": true}')
            gc.collect()
            os.fstat(0)  # OSError(EBADF) here means fd 0 was taken and closed
            assert client.recv(65536) == b'{"success": true}'


class TestThePipeFormIsUntouched:
    """`echo '{…}' | fraisier deploy-daemon --project=api` is documented.

    fd 0 there is a pipe, not a socket, so the result keeps going to stdout —
    which is also what keeps `StandardOutput=journal` carrying the record it
    has always carried under socket activation.
    """

    def test_result_json_still_goes_to_stdout(self) -> None:
        with patch("fraisier.daemon.execute_deployment_request") as execute:
            execute.return_value = deployment_result()
            run = CliRunner().invoke(
                main, ["deploy-daemon", "--project", "api"], input=REQUEST
            )

        assert run.exit_code == 0
        json_lines = [
            line for line in run.output.splitlines() if line.strip().startswith("{")
        ]
        assert len(json_lines) == 1, run.output
        payload = json.loads(json_lines[0])
        assert payload["success"] is True
        assert payload["version"] == "abc123"

    def test_journal_prose_is_unchanged(self) -> None:
        """The human lines the journal has always shown, still shown."""
        with patch("fraisier.daemon.execute_deployment_request") as execute:
            execute.return_value = deployment_result()
            run = CliRunner().invoke(
                main, ["deploy-daemon", "--project", "api"], input=REQUEST
            )

        assert "Deployment successful - Deployment completed" in run.output
        assert "Version: abc123" in run.output


class TestAnOlderClientStillParsesTheResult:
    """v0.63.0 guards the result path with `elif wait and response_data:`.

    A 0.63.0 CLI against a v0.64.0 daemon is the safe direction of the skew:
    the guard it already has now sees a non-empty response and parses it. This
    replays that guard's shape rather than installing 0.63.0.
    """

    def test_the_old_guard_shape_reads_the_new_payload(self) -> None:
        with accepted_connection() as client:
            with patch("fraisier.daemon.execute_deployment_request") as execute:
                execute.return_value = deployment_result()
                CliRunner().invoke(
                    main, ["deploy-daemon", "--project", "api"], input=REQUEST
                )
            response_data = read_result(client)

        wait = True
        assert wait and response_data  # the v0.63.0 guard, verbatim
        result = json.loads(response_data.decode("utf-8"))
        assert result["success"] is True
        assert result.get("message") == "Deployment completed"
        assert result.get("version") == "abc123"
        assert result.get("duration") == 45.5

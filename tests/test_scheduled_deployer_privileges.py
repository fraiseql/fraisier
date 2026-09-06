"""The scheduled deployer never asks for a privilege it cannot get (#382).

Both units that host a deploy — ``deploy-service.j2`` and
``fraisier-webhook.service.j2`` — set ``NoNewPrivileges=yes``, under which
``sudo`` exits 1 before it does anything. A deployer that spawns
``sudo systemctl enable`` therefore fails on every scheduled deploy that gets
as far as its timer, having already pulled code and installed dependencies.

These tests pin the *route*: which systemd actions reach which transport.
systemd itself is not available here, so the refusal cannot be reproduced; the
route is what the fix changes and what a regression would change back.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

from fraisier.deployers.scheduled import ScheduledDeployer


class FakeHelperSocket:
    """An ``AF_UNIX`` server speaking the systemctl-helper protocol.

    Records every request it is sent. *responder* maps a request to the JSON
    body to answer with; the default accepts everything.
    """

    def __init__(
        self,
        path: Path,
        responder: Callable[[dict], dict] | None = None,
    ) -> None:
        self.path = path
        self.requests: list[dict] = []
        self._responder = responder or (
            lambda _req: {"ok": True, "stdout": "", "stderr": "", "returncode": 0}
        )
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(8)
        self._sock.settimeout(0.05)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:  # socket closed
                return
            with conn:
                buf = bytearray()
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                if not buf:
                    continue
                try:
                    request = json.loads(bytes(buf).split(b"\n", 1)[0])
                except json.JSONDecodeError:  # pragma: no cover - defensive
                    continue
                self.requests.append(request)
                conn.sendall(json.dumps(self._responder(request)).encode() + b"\n")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    @property
    def actions(self) -> list[tuple[str, str]]:
        return [(r.get("action", ""), r.get("service", "")) for r in self.requests]


@pytest.fixture
def helper_socket(tmp_path):
    server = FakeHelperSocket(tmp_path / "systemctl.sock")
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def routed_to_helper(helper_socket):
    """Point the service manager at the fake helper, as a deploy unit does."""
    with (
        patch.dict(os.environ, {"FRAISIER_SYSTEMCTL_SOCKET": str(helper_socket.path)}),
        patch("platform.system", return_value="Linux"),
    ):
        yield helper_socket


def _spawned_argvs(mock_run) -> list[list[str]]:
    return [
        call[0][0]
        for call in mock_run.call_args_list
        if call[0] and isinstance(call[0][0], list)
    ]


class TestExecuteRoutesThroughTheHelper:
    def test_execute_sends_reload_enable_start_to_the_socket(self, routed_to_helper):
        deployer = ScheduledDeployer(
            {"fraise_name": "backup", "systemd_timer": "backup.timer"}
        )

        with (
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="ActiveState=active\n", stderr=""
            )
            result = deployer.execute()

        assert result.success is True
        assert routed_to_helper.actions == [
            ("daemon-reload", ""),
            ("enable", "backup.timer"),
            ("start", "backup.timer"),
        ]

    def test_execute_spawns_no_sudo(self, routed_to_helper):
        deployer = ScheduledDeployer(
            {"fraise_name": "backup", "systemd_timer": "backup.timer"}
        )

        with (
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="ActiveState=active\n", stderr=""
            )
            deployer.execute()

        for argv in _spawned_argvs(mock_run):
            assert argv[0] != "sudo", f"deploy spawned sudo: {argv}"

    def test_rollback_restarts_through_the_socket(self, routed_to_helper):
        deployer = ScheduledDeployer(
            {"fraise_name": "backup", "systemd_timer": "backup.timer"}
        )

        with (
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="ActiveState=active\n", stderr=""
            )
            result = deployer.rollback()

        assert result.success is True
        assert routed_to_helper.actions == [("restart", "backup.timer")]
        for argv in _spawned_argvs(mock_run):
            assert argv[0] != "sudo", f"rollback spawned sudo: {argv}"

    def test_a_refusing_helper_fails_the_deploy(self, tmp_path):
        """A host whose helper predates #382 refuses ``enable``. The deploy
        must fail with that reason, and say how to fix it."""

        def _refuse_enable(request: dict) -> dict:
            if request.get("action") == "enable":
                return {"ok": False, "error": "action not allowed: 'enable'"}
            return {"ok": True, "stdout": "", "stderr": "", "returncode": 0}

        server = FakeHelperSocket(tmp_path / "refuse.sock", responder=_refuse_enable)
        try:
            deployer = ScheduledDeployer(
                {"fraise_name": "backup", "systemd_timer": "backup.timer"}
            )
            with (
                patch.dict(os.environ, {"FRAISIER_SYSTEMCTL_SOCKET": str(server.path)}),
                patch("platform.system", return_value="Linux"),
                patch("fraisier.deployers.mixins.write_status"),
                patch("subprocess.run") as mock_run,
            ):
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="ActiveState=active\n", stderr=""
                )
                result = deployer.execute()
        finally:
            server.close()

        assert result.success is False
        assert result.error_message
        assert "action not allowed" in result.error_message
        assert "fraisier scaffold-install" in result.error_message, (
            "a host whose helper predates #382 refuses `enable`; the deploy "
            "log must say how to fix it, not just that a command exited 1"
        )
        # The timer never started, so nothing pretends the job is scheduled.
        assert ("start", "backup.timer") not in server.actions


class TestReadOnlyChecksStayUnprivileged:
    """``is-active`` and ``show`` need no privilege, so they must not acquire
    one: routing them through the manager would add a ``sudo`` fallback on a
    host with no helper socket, and a wrong answer where there is a correct
    one today."""

    def test_health_check_runs_plain_systemctl(self):
        deployer = ScheduledDeployer({"systemd_timer": "backup.timer"})
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="active\n")
            assert deployer.health_check() is True
        argv = mock_run.call_args[0][0]
        assert argv == ["systemctl", "is-active", "backup.timer"]

    def test_is_deployment_needed_runs_plain_systemctl(self):
        deployer = ScheduledDeployer({"systemd_timer": "backup.timer"})
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=3, stdout="inactive\n")
            assert deployer.is_deployment_needed() is True
        argv = mock_run.call_args[0][0]
        assert argv == ["systemctl", "is-active", "backup.timer"]


class TestNoSudoInDeployers:
    """The privilege boundary, stated once.

    The only ``sudo`` a deployer may spawn is the install-helper fallback in
    ``mixins.py``, which runs on the bootstrap path before the socket unit
    exists — and whose own docstring says it cannot work under
    ``NoNewPrivileges``.
    """

    def test_only_the_install_fallback_names_sudo(self):
        deployers_dir = (
            Path(__file__).resolve().parent.parent / "fraisier" / "deployers"
        )
        offenders: list[str] = []
        for path in sorted(deployers_dir.glob("*.py")):
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if '"sudo"' not in line and "'sudo'" not in line:
                    continue
                if path.name == "mixins.py" and "self.install_user" in line:
                    continue
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        assert not offenders, (
            "a deployer spawns sudo; deploy units set NoNewPrivileges so it "
            "exits 1 (#382). Route it through ServiceManager instead:\n"
            + "\n".join(offenders)
        )

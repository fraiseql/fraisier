"""`timeout:` bounds what can be bounded, and says the rest (#384).

``deployment_timeout`` delivers ``DeploymentTimeoutExpired`` with
``PyThreadState_SetAsyncExc``, which lands at the next bytecode boundary.
Nothing inside a C wait is interrupted — a ``psycopg`` wait inside a migration,
a ``readline()`` on a helper socket, a child-process wait. Neither helper-socket
client set a timeout at all, so a hung helper held the per-fraise lock and the
``deploying`` record for as long as it hung, and the exception fired at an
arbitrary later line: after a migration that by then succeeded, or inside a
running ``rollback()`` whose ``except Exception`` reported it as
``Rollback failed: DeploymentTimeoutExpired:``.

These tests pin the three halves of the answer: the sockets are bounded, a
timeout inside a rollback is reported as a timeout, and the mechanism's real
limit is written down where it cannot drift from the code.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import TYPE_CHECKING

import pytest

from fraisier.deployers.api import APIDeployer
from fraisier.errors import DeploymentError
from fraisier.timeout import (
    DeploymentTimeoutExpired,
    deployment_timeout,
    derived_timeout,
    remaining_budget,
)

if TYPE_CHECKING:
    from pathlib import Path


class SilentServer:
    """An ``AF_UNIX`` server that accepts a connection and never answers."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(8)
        self._sock.settimeout(0.05)
        self._held: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            # Held open, never written to: the client's read blocks forever
            # unless the client bounds it itself.
            self._held.append(conn)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        for conn in self._held:
            conn.close()
        self._sock.close()


@pytest.fixture
def silent_helper(tmp_path):
    server = SilentServer(tmp_path / "helper.sock")
    try:
        yield server
    finally:
        server.close()


def _deployer(**extra) -> APIDeployer:
    return APIDeployer(
        {
            "fraise_name": "my_api",
            "environment": "production",
            "app_path": "/var/www/api",
            **extra,
        }
    )


class TestTheBudgetIsReadable:
    def test_no_budget_outside_a_deploy(self):
        assert remaining_budget() is None

    def test_the_budget_shrinks(self):
        with deployment_timeout(60):
            first = remaining_budget()
            assert first is not None
            assert 59 < first <= 60
            time.sleep(0.05)
            second = remaining_budget()
        assert second is not None
        assert second < first

    def test_the_budget_is_released_on_exit(self):
        with deployment_timeout(60):
            pass
        assert remaining_budget() is None

    def test_the_budget_is_released_when_the_body_raises(self):
        with pytest.raises(ValueError), deployment_timeout(60):
            raise ValueError("boom")
        assert remaining_budget() is None


@pytest.fixture
def short_socket_bound(monkeypatch):
    """Shrink the helper-socket bound so a hang is proved in fractions of a second.

    The bound itself — that it comes from the deploy budget — is pinned
    separately; here the point is only that a silent helper stops being
    waited on.
    """
    monkeypatch.setattr("fraisier.timeout.remaining_budget", lambda: 0.2)
    monkeypatch.setattr("fraisier.timeout.MIN_DERIVED_TIMEOUT_S", 0.05)


class TestTheInstallHelperSocketIsBounded:
    def test_a_silent_helper_does_not_hang_the_install(
        self, silent_helper, short_socket_bound
    ):
        deployer = _deployer()
        started = time.monotonic()

        with pytest.raises(DeploymentError) as excinfo:
            deployer._install_via_socket(
                str(silent_helper.path), ["uv", "sync"], "/var/www/api"
            )

        elapsed = time.monotonic() - started
        assert elapsed < 5, f"blocked for {elapsed:.1f}s on a silent helper"
        assert "did not answer" in str(excinfo.value)
        assert str(silent_helper.path) in str(excinfo.value)

    def test_the_message_says_where_to_look(self, silent_helper, short_socket_bound):
        deployer = _deployer()
        with pytest.raises(DeploymentError) as excinfo:
            deployer._install_via_socket(
                str(silent_helper.path), ["uv", "sync"], "/var/www/api"
            )
        assert "journalctl" in str(excinfo.value)


class TestTheScaffoldInstallSocketIsBounded:
    def test_a_silent_helper_does_not_hang_the_scaffold_install(
        self, silent_helper, short_socket_bound, monkeypatch, tmp_path
    ):
        deployer = _deployer()
        monkeypatch.setattr(
            "fraisier.deployers.base._get_scaffold_socket_path",
            lambda _name: str(silent_helper.path),
        )
        config_path = tmp_path / "fraises.yaml"
        config_path.write_text("project_name: proj\nfraises: {}\n")
        started = time.monotonic()

        with pytest.raises(DeploymentError) as excinfo:
            deployer._try_scaffold_install_via_socket(config_path)

        elapsed = time.monotonic() - started
        assert elapsed < 5, f"blocked for {elapsed:.1f}s on a silent helper"
        assert "did not answer" in str(excinfo.value)

    def test_a_missing_socket_still_falls_back_quietly(self, monkeypatch, tmp_path):
        """A hang is not an absence. Only the hang is fatal: the helper
        accepted the connection, so running the install a second time through
        the subprocess fallback could install twice."""
        deployer = _deployer()
        monkeypatch.setattr(
            "fraisier.deployers.base._get_scaffold_socket_path",
            lambda _name: str(tmp_path / "absent.sock"),
        )
        config_path = tmp_path / "fraises.yaml"
        config_path.write_text("project_name: proj\nfraises: {}\n")

        assert deployer._try_scaffold_install_via_socket(config_path) is None


class TestASocketCallCannotOutliveTheBudget:
    def test_the_bound_comes_from_the_remaining_budget(self):
        with deployment_timeout(30):
            bounded = derived_timeout(300)
        assert 29 < bounded <= 30

    def test_a_call_outside_a_deploy_falls_back_to_its_own_default(self):
        assert derived_timeout(300) == 300

    def test_an_exhausted_budget_never_yields_a_non_blocking_socket(self, monkeypatch):
        """``settimeout(0)`` puts a socket in *non-blocking* mode, which would
        turn an exhausted budget into a busy failure on the first recv rather
        than a bounded wait. The floor exists for that, not for generosity."""
        monkeypatch.setattr("fraisier.timeout.remaining_budget", lambda: 0.0)
        assert derived_timeout(300) > 0


class TestATimeoutInsideARollbackIsATimeout:
    def test_rollback_lets_the_timeout_through(self, monkeypatch):
        """``rollback()`` used to swallow it into ``except Exception`` and
        report ``Rollback failed: DeploymentTimeoutExpired:`` — the deploy
        timed out, and the record blamed the rollback."""
        deployer = _deployer()
        deployer._previous_sha = "abc1234"

        def _boom(_target):
            raise DeploymentTimeoutExpired("Deployment timed out after 1 seconds")

        monkeypatch.setattr(deployer, "_git_rollback", _boom)

        with pytest.raises(DeploymentTimeoutExpired):
            deployer.rollback()

    def test_a_plain_rollback_failure_is_still_reported_as_one(self, monkeypatch):
        from fraisier.deployers.base import DeploymentStatus

        deployer = _deployer()
        deployer._previous_sha = "abc1234"

        def _boom(_target):
            raise RuntimeError("git is broken")

        monkeypatch.setattr(deployer, "_git_rollback", _boom)
        monkeypatch.setattr(
            "fraisier.deployers.mixins.write_status", lambda *_a, **_k: None
        )

        result = deployer.rollback()

        assert result.status == DeploymentStatus.ROLLBACK_FAILED
        assert "git is broken" in (result.error_message or "")

    def test_the_timeout_report_says_the_rollback_was_interrupted(self, monkeypatch):
        """The real sequence: a health-check rollback runs inside the timer,
        the timer fires during it, and `execute()`'s handler then rolls back
        again — this time to completion. The report must still say the first
        attempt was cut short."""
        deployer = _deployer()
        deployer._previous_sha = "abc1234"
        monkeypatch.setattr(
            "fraisier.deployers.mixins.write_status", lambda *_a, **_k: None
        )
        monkeypatch.setattr(deployer, "_restore_version_json", lambda: None)
        monkeypatch.setattr(deployer, "_notify", lambda _r: None)

        def _interrupted(_target):
            raise DeploymentTimeoutExpired("Deployment timed out after 1 seconds")

        monkeypatch.setattr(deployer, "_git_rollback", _interrupted)
        with pytest.raises(DeploymentTimeoutExpired):
            deployer.rollback()

        monkeypatch.setattr(deployer, "_git_rollback", lambda _target: None)
        monkeypatch.setattr(deployer, "_finalize_rollback", lambda *_a: _rolled_back())

        result = deployer._handle_timeout(
            DeploymentTimeoutExpired("Deployment timed out after 1 seconds"),
            old_version="deadbeef",
            start_time=time.time(),
        )

        assert "interrupted" in (result.error_message or "").lower(), (
            result.error_message
        )

    def test_an_uninterrupted_timeout_says_nothing_extra(self, monkeypatch):
        deployer = _deployer()
        monkeypatch.setattr(
            "fraisier.deployers.mixins.write_status", lambda *_a, **_k: None
        )
        monkeypatch.setattr(deployer, "_restore_version_json", lambda: None)

        result = deployer._handle_timeout(
            DeploymentTimeoutExpired("Deployment timed out after 1 seconds"),
            old_version="deadbeef",
            start_time=time.time(),
        )

        assert result.error_message == "Deployment timed out after 1 seconds"


def _rolled_back():
    from fraisier.deployers.base import DeploymentResult, DeploymentStatus

    return DeploymentResult(success=True, status=DeploymentStatus.ROLLED_BACK)


class TestWhatTheTimerCannotDo:
    """The limit, pinned so the documentation cannot drift from the code.

    ``PyThreadState_SetAsyncExc`` raises at the next bytecode boundary. A
    thread inside a C-level wait reaches no boundary until the wait returns, so
    ``timeout:`` is checked *between* steps: a step that blocks past it is
    reported when it returns, not interrupted.
    """

    def test_a_blocking_call_is_not_interrupted(self):
        started = time.monotonic()
        with pytest.raises(DeploymentTimeoutExpired), deployment_timeout(0.2):
            time.sleep(0.6)
        elapsed = time.monotonic() - started

        assert elapsed >= 0.6, (
            "the sleep was interrupted — if the mechanism has changed, the "
            "docstrings, docs/cli-reference.md and the config schema "
            "description that say otherwise must change with it"
        )

    def test_the_docstring_states_the_guarantee(self):
        doc = (deployment_timeout.__doc__ or "").lower()
        assert "between steps" in doc, doc

    def test_the_guide_states_the_guarantee(self):
        from pathlib import Path as _Path

        guide = (
            _Path(__file__).resolve().parent.parent / "docs" / "deployment-guide.md"
        ).read_text()
        assert "`timeout:` guarantees" in guide
        assert "between steps" in guide

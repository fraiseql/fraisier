"""Issue #246 regression: concurrent deploy during webhook self-upgrade.

These tests model the multi-environment-on-one-webhook topology where a
``ship dev``-then-``ship staging`` promotion races with the self-upgrade
worker's restart RPC. The drain coordination introduced in v0.31 must
defer the restart until in-flight ``*.lock`` files release.

The tests use real ``fcntl`` flocks via the ``flock_holder`` fixture and
real ``count_held_deployment_locks`` calls — only the systemctl helper
RPC is mocked (testing IPC against a live systemd is out of scope).
"""

from __future__ import annotations

import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from fraisier.locking import DRAINING_FLAG_NAME
from fraisier.webhook_self_upgrade import _DRAIN_TIMEOUT_RC, _run_upgrade


class TestConcurrentDeployNotKilled:
    """Restart is deferred until in-flight ``*.lock`` files release."""

    def test_restart_deferred_until_held_lock_releases(self, tmp_path, flock_holder):
        """Regression guard for #246: pre-fix ``_run_upgrade`` fired the restart
        RPC immediately after install; the drain-coordinated version must wait."""
        from types import SimpleNamespace

        proc, release = flock_holder("staging", lock_dir=tmp_path)
        restart_mock = MagicMock(
            return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
        )

        rc_holder: dict[str, int] = {}

        def run_worker() -> None:
            with (
                patch(
                    "fraisier.webhook_self_upgrade.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ),
                patch(
                    "fraisier.drain_restart._call_via_socket",
                    side_effect=restart_mock,
                ),
            ):
                rc_holder["rc"] = _run_upgrade(
                    "0.31.0",
                    "fraisier-myproj-webhook.service",
                    "/run/x.sock",
                    lock_dir=tmp_path,
                    drain_timeout_s=10,
                    drain_poll_s=0.05,
                    drain_settle_s=0.05,
                )

        worker = threading.Thread(target=run_worker)
        worker.start()
        # Allow the worker time to: touch flag, do (stubbed) install, settle, poll.
        # The lock is held, so restart_mock must not have fired.
        time.sleep(0.5)
        assert restart_mock.call_count == 0, (
            "restart fired while a *.lock was still held — drain coordination broken"
        )

        # Releasing the held flock allows the worker's drain loop to observe 0
        # and proceed to restart.
        release.set()
        proc.join(timeout=5)
        worker.join(timeout=5)

        assert restart_mock.call_count == 1
        assert restart_mock.call_args[0] == (
            "/run/x.sock",
            "restart",
            "fraisier-myproj-webhook.service",
        )
        assert rc_holder["rc"] == 0
        # Flag is gone after the worker returns.
        assert not (tmp_path / DRAINING_FLAG_NAME).exists()


class TestEndpointReturns503DuringDrain:
    """The webhook returns 503 while the worker holds the ``.draining`` flag."""

    def test_webhook_returns_503_when_drain_flag_set_by_worker(self, tmp_path):
        from fraisier.git import WebhookEvent
        from fraisier.webhook import app as webhook_app

        provider = MagicMock()
        provider.verify_webhook_signature.return_value = True
        provider.parse_webhook_event.return_value = WebhookEvent(
            provider="github",
            event_type="push",
            branch="main",
            commit_sha="abc123",
            sender="dev",
            is_push=True,
            is_ping=False,
        )

        cfg = MagicMock()
        cfg.get_fraises_for_branch.return_value = [
            {"fraise_name": "api", "environment": "staging", "type": "api"}
        ]
        cfg.deployment.lock_dir = str(tmp_path)
        cfg.webhook = {}
        cfg.get_git_provider_config.return_value = {"provider": "github"}

        with (
            patch("fraisier.webhook.get_provider", return_value=provider),
            patch("fraisier.webhook.get_config", return_value=cfg),
            TestClient(webhook_app) as client,
        ):
            # Lifespan startup clears stale flags — set ours *after* startup
            # so it models a worker that's already touched it.
            (tmp_path / DRAINING_FLAG_NAME).touch()
            response = client.post(
                "/webhook",
                json={"ref": "refs/heads/main"},
                headers={
                    "X-GitHub-Event": "push",
                    "X-GitHub-Delivery": "test-int-draining-001",
                },
            )

        assert response.status_code == 503
        assert response.headers["retry-after"] == "60"


class TestDrainTimeoutObservable:
    """Drain timeout skips the restart and logs the held lock basenames."""

    def test_drain_timeout_logs_and_skips_restart(self, tmp_path, flock_holder, caplog):
        from types import SimpleNamespace

        proc, release = flock_holder("staging", lock_dir=tmp_path)
        try:
            with (
                patch(
                    "fraisier.webhook_self_upgrade.subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ),
                patch("fraisier.drain_restart._call_via_socket") as mock_socket,
                caplog.at_level(
                    logging.WARNING, logger="fraisier.webhook_self_upgrade"
                ),
            ):
                rc = _run_upgrade(
                    "0.31.0",
                    "fraisier-myproj-webhook.service",
                    "/run/x.sock",
                    lock_dir=tmp_path,
                    drain_timeout_s=1,
                    drain_poll_s=0.05,
                    drain_settle_s=0.05,
                )
        finally:
            release.set()
            proc.join(timeout=5)

        assert rc == _DRAIN_TIMEOUT_RC
        mock_socket.assert_not_called()
        assert "drain timeout" in caplog.text
        assert "staging.lock" in caplog.text


# Marker so collectors group these as integration tests, mirroring the
# rest of tests/integration/.
pytestmark = pytest.mark.integration

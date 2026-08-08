"""The deploy that caused a deferral is what pays it back (#349).

`install.sh` records the restarts it did not perform; the webhook spawns the
drain worker at the end of the deploy, while the lock it must wait for is still
held. Skipped only when a self-upgrade is already draining, because both raise
the same single ``.draining`` flag.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fraisier import webhook as webhook_mod


class _Result:
    def __init__(self, success: bool):
        self.success = success
        self.new_version = "abc1234"
        self.old_version = "def5678"
        self.error_message = None if success else "boom"
        self.status = SimpleNamespace(value="success" if success else "failed")


def _config(tmp_path):
    return SimpleNamespace(
        project_name="testapp",
        webhook={},
        deployment=SimpleNamespace(lock_dir=str(tmp_path)),
        get_deploy_user=lambda *_a, **_k: "deployer",
    )


@pytest.fixture
def wiring(tmp_path, monkeypatch):
    """Drive `_run_deployment` with the deployer and side effects stubbed out."""
    calls = SimpleNamespace(deferred=[], upgrade=[])

    deployer = MagicMock()
    monkeypatch.setattr(webhook_mod, "get_config", lambda: _config(tmp_path))
    monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", "/run/x.sock")

    def _fake_deferred(**kwargs):
        calls.deferred.append(kwargs)

    def _fake_upgrade(*_a, **_k):
        calls.upgrade.append(True)
        return calls.upgrade_returns

    calls.upgrade_returns = False
    monkeypatch.setattr(webhook_mod, "maybe_apply_deferred_restarts", _fake_deferred)
    monkeypatch.setattr(webhook_mod, "maybe_self_upgrade", _fake_upgrade)
    calls.deployer = deployer
    calls.tmp_path = tmp_path
    return calls


async def _run(wiring, *, success: bool):
    wiring.deployer.execute.return_value = _Result(success)
    # Patch the name `_run_deployment` imports, not `APIDeployer.__new__`:
    # restoring a patched `__new__` leaves a real attribute on the class where
    # there was only an inherited one, and every later test that builds an
    # APIDeployer inherits that.
    with patch("fraisier.deployers.api.APIDeployer", return_value=wiring.deployer):
        await webhook_mod._run_deployment(
            "api",
            "production",
            {"type": "api", "app_path": str(wiring.tmp_path / "app")},
            None,
            "main",
            "abc1234",
            MagicMock(),
        )


class TestDeferredRestartsArePaid:
    @pytest.mark.asyncio
    async def test_paid_after_a_successful_deploy(self, wiring):
        await _run(wiring, success=True)
        assert len(wiring.deferred) == 1
        assert str(wiring.deferred[0]["lock_dir"]) == str(wiring.tmp_path)
        assert wiring.deferred[0]["socket_path"] == "/run/x.sock"

    @pytest.mark.asyncio
    async def test_paid_after_a_failed_deploy_too(self, wiring):
        """The units were installed either way; a rollback makes it a no-op."""
        await _run(wiring, success=False)
        assert len(wiring.deferred) == 1

    @pytest.mark.asyncio
    async def test_skipped_while_a_self_upgrade_is_draining(self, wiring):
        """Two workers would fight over the one `.draining` flag."""
        wiring.upgrade_returns = True
        await _run(wiring, success=True)
        # `_run_deployment` swallows exceptions, so an empty list would also be
        # what a crashed deploy looks like. Prove the path was reached first.
        assert wiring.upgrade == [True]
        assert wiring.deferred == []

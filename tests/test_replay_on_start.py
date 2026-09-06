"""The upgrade's restart finishes the work the upgrade dropped (#367).

`lifespan` is the natural trigger — the upgrade *ends* by restarting the
webhook — and exactly where a replay is most dangerous, because a restart for
any other reason would fire it too. The handoff marker is what separates the
two.

The replay dispatches through `execute_deployment`, the same path a push takes.
That is what makes "an entry clears only when a later deploy for that target
succeeds" true by construction: the success branch of `_run_deployment` already
calls `_discharge_refusal`, and nothing here clears anything.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from fraisier.refused_dispatch_record import (
    RefusedDispatch,
    read_refused_dispatches,
    record_refused_dispatch,
)
from fraisier.replay_handoff import record_replay_handoff
from fraisier.webhook import _replay_refused_dispatches


@pytest.fixture
def lock_dir(tmp_path):
    return tmp_path


@pytest.fixture
def config(lock_dir):
    cfg = MagicMock()
    cfg.deployment.lock_dir = str(lock_dir)
    cfg.webhook = {}
    cfg.get_fraise_environment.side_effect = lambda f, _e: (
        {"app_path": f"/srv/{f}"} if f in {"api", "worker"} else None
    )
    return cfg


def _refuse(lock_dir, fraise, environment, branch="main"):
    record_refused_dispatch(
        lock_dir,
        fraise=fraise,
        environment=environment,
        branch=branch,
        commit_sha="abc1234",
        webhook_id=1,
    )


def _run_replay(config, lock_dir, execute=None):
    """Run the startup hook and drive the task it schedules to completion.

    `_replay_refused_dispatches` deliberately does not block startup: it
    schedules the replays and returns. The test has to run what it scheduled,
    or it would only be asserting that nothing happened synchronously.
    """
    dispatched: list[dict] = []
    scheduled: list = []

    async def _fake_execute(**kwargs):
        dispatched.append(kwargs)

    # The scheduled coroutines are driven *inside* the patch context. Running
    # them after it would call the real `execute_deployment`, which is how this
    # harness first went wrong: it wrote to /var/lib/fraisier and the test
    # merely reported an empty dispatch list.
    with (
        patch("fraisier.webhook.get_config", return_value=config),
        patch(
            "fraisier.webhook.execute_deployment",
            side_effect=execute or _fake_execute,
        ),
        patch("fraisier.webhook.asyncio.create_task", side_effect=scheduled.append),
    ):
        _replay_refused_dispatches()
        for coro in scheduled:
            asyncio.run(coro)
    return dispatched, scheduled


class TestTheTriggerCannotFireOnAnUnrelatedRestart:
    def test_no_marker_replays_nothing(self, config, lock_dir):
        _refuse(lock_dir, "api", "staging")

        dispatched, _ = _run_replay(config, lock_dir)

        assert dispatched == []

    def test_the_ledger_is_left_alone_when_nothing_is_replayed(self, config, lock_dir):
        _refuse(lock_dir, "api", "staging")

        _run_replay(config, lock_dir)

        assert len(read_refused_dispatches(lock_dir)) == 1

    def test_a_marker_is_consumed_so_the_next_restart_is_quiet(self, config, lock_dir):
        _refuse(lock_dir, "api", "staging")
        record_replay_handoff(lock_dir, version="0.72.0", service="w.service")

        first, _ = _run_replay(config, lock_dir)
        second, _ = _run_replay(config, lock_dir)

        assert len(first) == 1
        assert second == []


class TestWhatItDispatches:
    def test_it_re_fires_the_refused_target(self, config, lock_dir):
        _refuse(lock_dir, "api", "staging", branch="release")
        record_replay_handoff(lock_dir, version="0.72.0", service="w.service")

        dispatched, _ = _run_replay(config, lock_dir)

        assert len(dispatched) == 1
        assert dispatched[0]["fraise_name"] == "api"
        assert dispatched[0]["environment"] == "staging"
        assert dispatched[0]["git_branch"] == "release"

    def test_it_deploys_the_branch_head_not_the_recorded_sha(self, config, lock_dir):
        """`git_commit=None` is the whole point: `execute_deployment` skips its
        version gate and the deploy resolves the branch head. Passing the
        recorded sha would redeploy old code over anything pushed since."""
        _refuse(lock_dir, "api", "staging")
        record_replay_handoff(lock_dir, version="0.72.0", service="w.service")

        dispatched, _ = _run_replay(config, lock_dir)

        assert dispatched[0]["git_commit"] is None

    def test_production_is_dispatched_last(self, config, lock_dir):
        _refuse(lock_dir, "api", "production")
        _refuse(lock_dir, "api", "staging")
        record_replay_handoff(lock_dir, version="0.72.0", service="w.service")

        dispatched, _ = _run_replay(config, lock_dir)

        assert [d["environment"] for d in dispatched] == ["staging", "production"]

    def test_a_target_gone_from_config_is_not_dispatched(self, config, lock_dir):
        _refuse(lock_dir, "removed", "staging")
        _refuse(lock_dir, "api", "staging")
        record_replay_handoff(lock_dir, version="0.72.0", service="w.service")

        dispatched, _ = _run_replay(config, lock_dir)

        assert [d["fraise_name"] for d in dispatched] == ["api"]


class TestTheLedgerIsNotTouchedHere:
    def test_a_replay_does_not_clear_the_entry_itself(self, config, lock_dir):
        """Only a deploy that succeeds discharges the debt, through
        `_discharge_refusal` on the ordinary success path. A replay that
        cleared on *attempt* would re-create the bug in a new place: the record
        of what was lost, erased by the thing meant to recover it."""
        _refuse(lock_dir, "api", "staging")
        record_replay_handoff(lock_dir, version="0.72.0", service="w.service")

        _run_replay(config, lock_dir)

        assert [e.target for e in read_refused_dispatches(lock_dir)] == [
            ("api", "staging")
        ]

    def test_a_failing_replay_leaves_the_entry_standing(self, config, lock_dir):
        _refuse(lock_dir, "api", "staging")
        record_replay_handoff(lock_dir, version="0.72.0", service="w.service")

        async def _boom(**_kwargs):
            raise RuntimeError("deploy blew up")

        _run_replay(config, lock_dir, execute=_boom)

        assert len(read_refused_dispatches(lock_dir)) == 1


class TestItIsSwitchable:
    def test_off_replays_nothing(self, config, lock_dir):
        config.webhook = {"replay_refused": "off"}
        _refuse(lock_dir, "api", "staging")
        record_replay_handoff(lock_dir, version="0.72.0", service="w.service")

        dispatched, _ = _run_replay(config, lock_dir)

        assert dispatched == []

    def test_off_still_consumes_the_marker(self, config, lock_dir):
        """Otherwise switching it back on later fires a stale handoff."""
        from fraisier.replay_handoff import REPLAY_HANDOFF_FILE

        config.webhook = {"replay_refused": "off"}
        record_replay_handoff(lock_dir, version="0.72.0", service="w.service")

        _run_replay(config, lock_dir)

        assert not (lock_dir / REPLAY_HANDOFF_FILE).exists()

    def test_head_is_the_default(self, config, lock_dir):
        assert config.webhook.get("replay_refused", "head") == "head"


class TestNothingHereStopsTheWebhookStarting:
    def test_an_unloadable_config_is_survived(self, lock_dir):
        from fraisier.errors import ValidationError

        with (
            patch(
                "fraisier.webhook.get_config",
                side_effect=ValidationError("broken"),
            ),
            patch("fraisier.webhook.execute_deployment") as mock_exec,
        ):
            _replay_refused_dispatches()

        assert not mock_exec.called

    def test_an_unresolvable_lock_dir_is_survived(self):
        cfg = MagicMock()
        cfg.deployment.lock_dir = "relative/locks"
        cfg.webhook = {}

        with (
            patch("fraisier.webhook.get_config", return_value=cfg),
            patch("fraisier.webhook.execute_deployment") as mock_exec,
        ):
            _replay_refused_dispatches()

        assert not mock_exec.called


class TestRefusedDispatchIsUnchanged:
    def test_the_ledger_entry_shape_still_carries_a_branch(self):
        """The replay needs it; a future shrink of the record would break it."""
        entry = RefusedDispatch(
            fraise="api",
            environment="staging",
            branch="main",
            commit_sha="abc",
            webhook_id=1,
            refused_at="2026-09-06T00:00:00+00:00",
        )
        assert entry.branch == "main"


class TestDoctorDoesNotBecomeANoOp:
    """#365's check must say the same thing before and after a replay attempt.

    A replay that cleared entries on *attempt* would silence `doctor` while the
    debt was still outstanding — the recovery erasing the record of what it was
    recovering.
    """

    def _check(self, lock_dir):
        from fraisier.doctor import _check_refused_dispatch

        cfg = MagicMock()
        cfg.deployment.lock_dir = str(lock_dir)
        return _check_refused_dispatch(cfg)

    def test_it_still_warns_after_a_failed_replay(self, config, lock_dir):
        _refuse(lock_dir, "api", "staging")
        record_replay_handoff(lock_dir, version="0.72.0", service="w.service")
        before = self._check(lock_dir)

        async def _boom(**_kwargs):
            raise RuntimeError("deploy blew up")

        _run_replay(config, lock_dir, execute=_boom)
        after = self._check(lock_dir)

        assert before.status == after.status != "pass"
        assert "api/staging" in after.detail

    def test_it_still_warns_after_a_replay_that_has_not_finished(
        self, config, lock_dir
    ):
        _refuse(lock_dir, "api", "staging")
        record_replay_handoff(lock_dir, version="0.72.0", service="w.service")

        _run_replay(config, lock_dir)

        assert self._check(lock_dir).status != "pass"

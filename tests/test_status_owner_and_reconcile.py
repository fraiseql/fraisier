"""A deploy that is killed must still leave a record (#349).

The webhook is SIGKILLed at systemd's stop timeout when something restarts it
mid-deploy. The kernel releases the flock, so the *lock* recovers — but the
status file is left saying ``deploying`` forever, and `fraisier status` paints
that blue with an ever-growing elapsed time. Nothing distinguishes a deploy in
progress from one whose process no longer exists.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from fraisier.status import (
    FAILURE_STATES,
    NON_TERMINAL_STATES,
    DeploymentStatusFile,
    current_owner,
    owner_is_gone,
    read_status,
    reconcile_orphaned_deploys,
    write_status,
)


def _read(tmp_path, name="api") -> DeploymentStatusFile:
    status = read_status(name, status_dir=tmp_path)
    assert status is not None, f"no status file for {name}"
    return status


def _status(tmp_path, **kwargs):
    base: dict[str, Any] = {
        "fraise_name": "api",
        "environment": "production",
        "state": "deploying",
        "started_at": "2026-08-08T19:11:58",
    }
    base.update(kwargs)
    status = DeploymentStatusFile(**base)
    write_status(status, status_dir=tmp_path)
    return status


class TestOwnerIdentity:
    def test_current_owner_records_this_process(self):
        owner = current_owner()
        assert owner["owner_pid"] == os.getpid()

    def test_boot_id_is_recorded_when_the_kernel_exposes_one(self):
        owner = current_owner()
        # Linux exposes /proc/sys/kernel/random/boot_id; elsewhere None is
        # honest rather than fabricated.
        assert "owner_boot_id" in owner

    def test_invocation_id_comes_from_systemd(self, monkeypatch):
        monkeypatch.setenv("INVOCATION_ID", "cafe1234")
        assert current_owner()["owner_invocation_id"] == "cafe1234"

    def test_absent_invocation_id_is_none_not_empty(self, monkeypatch):
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        assert current_owner()["owner_invocation_id"] is None


class TestOwnerIsGone:
    def test_a_different_boot_is_definitive(self):
        status = DeploymentStatusFile(
            fraise_name="api",
            environment="production",
            state="deploying",
            owner_pid=os.getpid(),
            owner_boot_id="not-this-boot",
        )
        assert owner_is_gone(status) is True

    def test_a_live_pid_on_this_boot_is_not_gone(self):
        status = DeploymentStatusFile(
            fraise_name="api",
            environment="production",
            state="deploying",
            **current_owner(),
        )
        assert owner_is_gone(status) is False

    def test_a_dead_pid_is_gone(self):
        owner = current_owner()
        # PID 2^22 + 1 is above the default pid_max, so it cannot exist.
        owner["owner_pid"] = 4194305
        status = DeploymentStatusFile(
            fraise_name="api", environment="production", state="deploying", **owner
        )
        assert owner_is_gone(status) is True

    def test_no_owner_recorded_is_not_provably_gone(self):
        """Status files written before this release carry no identity."""
        status = DeploymentStatusFile(
            fraise_name="api", environment="production", state="deploying"
        )
        assert owner_is_gone(status) is False


class TestInterruptedIsAFailure:
    def test_interrupted_is_in_failure_states(self):
        assert "interrupted" in FAILURE_STATES

    def test_deploying_and_pending_are_the_non_terminal_states(self):
        assert frozenset({"pending", "deploying"}) == NON_TERMINAL_STATES

    def test_no_state_is_both_terminal_and_non_terminal(self):
        assert not (FAILURE_STATES & NON_TERMINAL_STATES)


class TestReconcileOrphanedDeploys:
    def test_a_dead_owner_becomes_interrupted(self, tmp_path):
        owner = current_owner()
        owner["owner_pid"] = 4194305
        _status(tmp_path, **owner)

        changed = reconcile_orphaned_deploys(status_dir=tmp_path)

        assert changed == ["api"]
        assert _read(tmp_path).state == "interrupted"

    def test_the_record_says_the_deploy_never_reported(self, tmp_path):
        owner = current_owner()
        owner["owner_pid"] = 4194305
        _status(tmp_path, **owner)
        reconcile_orphaned_deploys(status_dir=tmp_path)

        status = _read(tmp_path)
        assert "did not report" in (status.error_message or "").lower()
        assert status.finished_at is not None
        assert status.started_at == "2026-08-08T19:11:58"

    def test_a_live_owner_is_left_alone(self, tmp_path):
        _status(tmp_path, **current_owner())
        assert reconcile_orphaned_deploys(status_dir=tmp_path) == []
        assert _read(tmp_path).state == "deploying"

    def test_a_terminal_state_is_never_rewritten(self, tmp_path):
        owner = current_owner()
        owner["owner_pid"] = 4194305
        _status(tmp_path, state="success", **owner)
        assert reconcile_orphaned_deploys(status_dir=tmp_path) == []
        assert _read(tmp_path).state == "success"

    def test_a_missing_status_dir_is_not_an_error(self, tmp_path):
        assert reconcile_orphaned_deploys(status_dir=tmp_path / "nope") == []

    def test_an_unparseable_status_file_is_skipped_not_fatal(self, tmp_path):
        (tmp_path / "api.status.json").write_text("{not json")
        owner = current_owner()
        owner["owner_pid"] = 4194305
        _status(tmp_path, fraise_name="other", **owner)
        assert reconcile_orphaned_deploys(status_dir=tmp_path) == ["other"]


class TestStatusFileWireFormat:
    def test_read_status_ignores_keys_it_does_not_know(self, tmp_path):
        """A host mid-self-upgrade runs two fraisier versions; the reader must
        not crash on a field the writer added."""
        (tmp_path / "api.status.json").write_text(
            json.dumps(
                {
                    "fraise_name": "api",
                    "environment": "production",
                    "state": "success",
                    "some_future_field": 1,
                }
            )
        )
        status = read_status("api", status_dir=tmp_path)
        assert status is not None
        assert status.state == "success"

    def test_a_file_without_owner_fields_still_loads(self, tmp_path):
        (tmp_path / "api.status.json").write_text(
            json.dumps(
                {
                    "fraise_name": "api",
                    "environment": "production",
                    "state": "deploying",
                }
            )
        )
        status = read_status("api", status_dir=tmp_path)
        assert status is not None
        assert status.owner_pid is None


class TestConsumersHandleInterrupted:
    def test_cli_renders_interrupted_loudly(self, tmp_path, monkeypatch):
        from fraisier.cli import _info

        owner = current_owner()
        owner["owner_pid"] = 4194305
        _status(tmp_path, state="interrupted", **owner)
        monkeypatch.setattr(
            _info, "read_status", lambda name: read_status(name, status_dir=tmp_path)
        )
        rendered = _info._compute_deployment_state("api", "abc", "abc")
        assert "interrupt" in rendered.lower()
        assert "red" in rendered

    def test_cli_does_not_show_a_dead_deploy_as_deploying(self, tmp_path, monkeypatch):
        """The blue `deploying (86400s)` is the symptom the issue describes."""
        from fraisier.cli import _info

        owner = current_owner()
        owner["owner_pid"] = 4194305
        _status(tmp_path, **owner)
        monkeypatch.setattr(
            _info, "read_status", lambda name: read_status(name, status_dir=tmp_path)
        )
        rendered = _info._compute_deployment_state("api", "abc", "abc")
        assert "deploying" not in rendered.lower()

    def test_webhook_failure_details_answer_for_interrupted(self):
        """`/api/status/{fraise}/details` gates on FAILURE_STATES, so membership
        is what stops it answering 'No failure to report' (#293's consumer)."""
        assert "interrupted" in FAILURE_STATES


class TestDeployersRecordTheirIdentity:
    def test_write_status_stamps_the_owner(self, tmp_path):
        from fraisier.deployers.mixins import GitDeployMixin

        class _D(GitDeployMixin):
            def __init__(self):
                self.fraise_name = "api"
                self.environment = "production"
                self.status_dir = tmp_path

        _D()._write_status("deploying")
        assert _read(tmp_path).owner_pid == os.getpid()


@pytest.mark.parametrize("state", ["pending", "deploying"])
def test_every_non_terminal_state_is_reconciled(tmp_path, state):
    owner = current_owner()
    owner["owner_pid"] = 4194305
    _status(tmp_path, state=state, **owner)
    assert reconcile_orphaned_deploys(status_dir=tmp_path) == ["api"]

"""Tests for the ledger a refused dispatch leaves behind (#365).

The webhook answers 503 while a self-upgrade drains, which is correct
back-pressure. What was not correct is that the request then vanished: no
file, no row, nothing in ``fraisier health`` or ``deployment-status``. The
branch simply stayed undeployed and looked like one nobody had pushed.

This ledger inherits ``self_upgrade_record``'s rule exactly — an entry is
cleared only when a later deploy for that target *succeeds* — so a debt
nobody paid cannot look settled.
"""

from __future__ import annotations

import json

import pytest

from fraisier.refused_dispatch_record import (
    REFUSED_DISPATCH_FILE,
    clear_refused_dispatch,
    read_refused_dispatches,
    record_refused_dispatch,
)


def _record(lock_dir, fraise="api", environment="staging", branch="main", sha="abc123"):
    record_refused_dispatch(
        lock_dir,
        fraise=fraise,
        environment=environment,
        branch=branch,
        commit_sha=sha,
        webhook_id=7,
    )


class TestRoundTrip:
    def test_absent_file_reads_as_empty(self, tmp_path):
        assert read_refused_dispatches(tmp_path) == []

    def test_a_refusal_round_trips(self, tmp_path):
        _record(tmp_path)
        [entry] = read_refused_dispatches(tmp_path)
        assert entry.fraise == "api"
        assert entry.environment == "staging"
        assert entry.branch == "main"
        assert entry.commit_sha == "abc123"
        assert entry.webhook_id == 7
        assert entry.refused_at

    def test_the_file_is_dot_prefixed(self, tmp_path):
        """``count_held_deployment_locks`` globs ``*.lock`` in this directory.

        A stray match changes the answer to "is a deploy in flight", which is
        why ``.self-upgrade-failure`` and ``.deferred-restarts`` are hidden
        too.
        """
        _record(tmp_path)
        assert REFUSED_DISPATCH_FILE.startswith(".")
        assert not list(tmp_path.glob("*.lock"))
        assert (tmp_path / REFUSED_DISPATCH_FILE).exists()


class TestDedupAndCap:
    def test_the_same_target_replaces_rather_than_appends(self, tmp_path):
        """The operator needs to know a target is behind, not how many times.

        The newest entry also carries the newest sha, which is the one worth
        deploying.
        """
        _record(tmp_path, sha="old")
        _record(tmp_path, sha="new")
        [entry] = read_refused_dispatches(tmp_path)
        assert entry.commit_sha == "new"

    def test_a_different_environment_is_a_different_entry(self, tmp_path):
        """One host serves several targets, each refusable independently.

        This is the reported scenario: two environments on one webhook host.
        """
        _record(tmp_path, environment="staging")
        _record(tmp_path, environment="production")
        assert {e.environment for e in read_refused_dispatches(tmp_path)} == {
            "staging",
            "production",
        }

    def test_a_different_fraise_is_a_different_entry(self, tmp_path):
        _record(tmp_path, fraise="api")
        _record(tmp_path, fraise="web")
        assert len(read_refused_dispatches(tmp_path)) == 2

    def test_the_dedup_key_is_not_the_branch(self, tmp_path):
        """Two pushes to different branches of one target are still one thing.

        Re-firing means deploying the latest, not replaying both.
        """
        _record(tmp_path, branch="main", sha="a")
        _record(tmp_path, branch="hotfix", sha="b")
        [entry] = read_refused_dispatches(tmp_path)
        assert entry.branch == "hotfix"

    def test_the_list_is_capped_oldest_dropped(self, tmp_path):
        """It is written from a request path into a runtime directory.

        Twenty distinct targets on one webhook host is already far past
        plausible; unbounded growth there is a liability.
        """
        for i in range(25):
            _record(tmp_path, fraise=f"f{i:02d}")
        entries = read_refused_dispatches(tmp_path)
        assert len(entries) == 20
        names = {e.fraise for e in entries}
        assert "f00" not in names
        assert "f24" in names


class TestClearing:
    def test_clearing_removes_only_that_target(self, tmp_path):
        _record(tmp_path, fraise="api", environment="staging")
        _record(tmp_path, fraise="api", environment="production")
        clear_refused_dispatch(tmp_path, fraise="api", environment="staging")
        [entry] = read_refused_dispatches(tmp_path)
        assert entry.environment == "production"

    def test_clearing_the_last_entry_removes_the_file(self, tmp_path):
        _record(tmp_path)
        clear_refused_dispatch(tmp_path, fraise="api", environment="staging")
        assert not (tmp_path / REFUSED_DISPATCH_FILE).exists()

    def test_clearing_an_unrecorded_target_is_a_noop(self, tmp_path):
        _record(tmp_path)
        clear_refused_dispatch(tmp_path, fraise="other", environment="staging")
        assert len(read_refused_dispatches(tmp_path)) == 1

    def test_clearing_with_no_file_does_not_raise(self, tmp_path):
        clear_refused_dispatch(tmp_path, fraise="api", environment="staging")


class TestToleranceOnRead:
    """``doctor`` must not be taken down by the file it exists to read."""

    def test_malformed_json_reads_as_empty_and_warns(self, tmp_path, caplog):
        import logging

        (tmp_path / REFUSED_DISPATCH_FILE).write_text("{not json")
        with caplog.at_level(logging.WARNING):
            assert read_refused_dispatches(tmp_path) == []
        assert "not valid JSON" in caplog.text

    def test_a_truncated_list_reads_as_empty(self, tmp_path):
        (tmp_path / REFUSED_DISPATCH_FILE).write_text('[{"fraise": "api"')
        assert read_refused_dispatches(tmp_path) == []

    def test_a_non_list_payload_reads_as_empty(self, tmp_path):
        (tmp_path / REFUSED_DISPATCH_FILE).write_text('{"fraise": "api"}')
        assert read_refused_dispatches(tmp_path) == []

    def test_unknown_keys_are_ignored(self, tmp_path):
        """A record written by a newer fraisier is normal, not an error.

        A self-upgrade puts two versions on one host by design.
        """
        (tmp_path / REFUSED_DISPATCH_FILE).write_text(
            json.dumps(
                [
                    {
                        "fraise": "api",
                        "environment": "staging",
                        "branch": "main",
                        "commit_sha": "abc",
                        "webhook_id": 1,
                        "refused_at": "2026-08-31T10:28:50+00:00",
                        "replay_attempts": 3,
                    }
                ]
            )
        )
        [entry] = read_refused_dispatches(tmp_path)
        assert entry.fraise == "api"

    def test_a_bad_entry_is_dropped_and_the_good_ones_survive(self, tmp_path):
        (tmp_path / REFUSED_DISPATCH_FILE).write_text(
            json.dumps(["nonsense", {"fraise": "api", "environment": "staging"}])
        )
        [entry] = read_refused_dispatches(tmp_path)
        assert entry.fraise == "api"


class TestBestEffort:
    """It runs underneath a response the webhook still has to send."""

    def test_an_unwritable_dir_warns_and_does_not_raise(self, tmp_path, caplog):
        import logging

        missing = tmp_path / "nope" / "deeper"
        with caplog.at_level(logging.WARNING):
            _record(missing)
        assert "could not record" in caplog.text

    def test_reading_an_unreadable_file_warns_and_does_not_raise(
        self, tmp_path, caplog, monkeypatch
    ):
        import logging
        from pathlib import Path

        _record(tmp_path)
        real = Path.read_text

        def boom(self, *a, **kw):
            if self.name == REFUSED_DISPATCH_FILE:
                raise PermissionError(13, "nope")
            return real(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", boom)
        with caplog.at_level(logging.WARNING):
            assert read_refused_dispatches(tmp_path) == []
        assert "could not read" in caplog.text

    def test_clearing_an_unwritable_file_warns_and_does_not_raise(
        self, tmp_path, caplog, monkeypatch
    ):
        import logging
        from pathlib import Path

        # Two entries, so clearing rewrites the file rather than unlinking it.
        _record(tmp_path, environment="staging")
        _record(tmp_path, environment="production")
        real = Path.write_text

        def boom(self, *a, **kw):
            if self.name == REFUSED_DISPATCH_FILE:
                raise PermissionError(13, "nope")
            return real(self, *a, **kw)

        monkeypatch.setattr(Path, "write_text", boom)
        with caplog.at_level(logging.WARNING):
            clear_refused_dispatch(tmp_path, fraise="api", environment="staging")
        assert "could not clear" in caplog.text


@pytest.mark.parametrize("lock_dir_type", [str, "path"])
def test_lock_dir_accepts_str_or_path(tmp_path, lock_dir_type):
    """Mirrors ``self_upgrade_record``, whose callers pass both."""
    target = str(tmp_path) if lock_dir_type is str else tmp_path
    record_refused_dispatch(
        target,
        fraise="api",
        environment="staging",
        branch="main",
        commit_sha="abc",
        webhook_id=1,
    )
    assert len(read_refused_dispatches(target)) == 1


class TestTheDoctorCheck:
    """A dropped request must be visible somewhere a human looks.

    Mirrors ``self_upgrade_failure``: ``warn``, not ``fail``, because the host
    is up and serving. What it has lost is a request.
    """

    def _run(self, config):
        from fraisier.doctor import DOCTOR_CHECKS

        return DOCTOR_CHECKS["refused_dispatch"].fn(config)

    def _config(self, lock_dir):
        from types import SimpleNamespace

        return SimpleNamespace(deployment=SimpleNamespace(lock_dir=str(lock_dir)))

    def test_it_is_registered(self):
        from fraisier.doctor import DOCTOR_CHECKS

        assert "refused_dispatch" in DOCTOR_CHECKS

    def test_no_config_skips(self):
        assert self._run(None).status == "skip"

    def test_no_lock_dir_configured_skips(self):
        from types import SimpleNamespace

        config = SimpleNamespace(deployment=SimpleNamespace(lock_dir=None))
        assert self._run(config).status == "skip"

    def test_a_clean_host_passes(self, tmp_path):
        assert self._run(self._config(tmp_path)).status == "pass"

    def test_a_standing_entry_warns_and_names_the_target(self, tmp_path):
        _record(tmp_path, fraise="api", environment="staging", branch="main")
        result = self._run(self._config(tmp_path))
        assert result.status == "warn"
        assert "api/staging" in result.detail
        assert "main" in result.detail

    def test_the_hint_is_the_command_that_re_fires_it(self, tmp_path):
        """Re-firing manually is exactly what the reporter did, and it worked."""
        _record(tmp_path, fraise="api", environment="staging", branch="main")
        hint = self._run(self._config(tmp_path)).fix_hint
        assert "fraisier trigger-deploy api staging --branch main" in hint

    def test_the_hint_quotes_a_hostile_branch_name(self, tmp_path):
        """Every field here came off a webhook payload.

        Git permits ``;``, ``&`` and ``$`` in a ref name, and this hint is
        written to be copy-pasted into a shell — so an unquoted branch is a
        second command waiting for an operator to paste it.
        """
        _record(tmp_path, branch="main;whoami")
        hint = self._run(self._config(tmp_path)).fix_hint
        assert "--branch 'main;whoami'" in hint
        assert "--branch main;whoami" not in hint

    def test_two_targets_are_both_named(self, tmp_path):
        _record(tmp_path, environment="staging")
        _record(tmp_path, environment="production")
        detail = self._run(self._config(tmp_path)).detail
        assert "api/staging" in detail
        assert "api/production" in detail

    def test_a_malformed_ledger_does_not_take_doctor_down(self, tmp_path):
        (tmp_path / REFUSED_DISPATCH_FILE).write_text("{not json")
        assert self._run(self._config(tmp_path)).status == "pass"

    def test_the_hint_says_whether_the_upgrade_is_still_running(self, tmp_path):
        """Tells "still draining, wait" from "long over, re-fire now"."""
        from fraisier.locking import DRAINING_FLAG_NAME

        _record(tmp_path)
        (tmp_path / DRAINING_FLAG_NAME).touch()
        hint = self._run(self._config(tmp_path)).fix_hint
        assert "still up" in hint

    def test_the_hint_omits_the_flag_when_it_is_gone(self, tmp_path):
        _record(tmp_path)
        hint = self._run(self._config(tmp_path)).fix_hint
        assert "still up" not in hint


class TestItIsDocumented:
    def test_doctor_md_covers_the_check(self):
        import pathlib as _pathlib

        doc = (
            _pathlib.Path(__file__).resolve().parent.parent / "docs" / "doctor.md"
        ).read_text()
        assert "refused_dispatch" in doc

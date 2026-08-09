"""A failed self-upgrade has to leave state, not just a line in a file (#351).

When ``uv tool install --force`` fails partway it leaves the tool venv
half-removed — ``bin/`` gone, ``lib/`` intact — so every
``~/.local/bin/fraisier*`` symlink dangles, including the one the webhook unit
names in ``ExecStart=``. The running process outlives its deleted binary, so
nothing looks wrong until the next restart fails 203/EXEC.

The only record was a file under ``/var/lib/fraisier/self-upgrade/`` that
nothing surfaces. This is the ledger ``doctor`` reads instead, in the same shape
v0.63.0 gave deferred restarts: **the entry is cleared only when a later upgrade
succeeds**, so a debt nobody paid stays visible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fraisier.self_upgrade_record import (
    SELF_UPGRADE_FAILURE_FILE,
    clear_self_upgrade_failure,
    read_self_upgrade_failure,
    record_self_upgrade_failure,
)


@pytest.fixture
def lock_dir(tmp_path):
    d = tmp_path / "run-fraisier"
    d.mkdir()
    return d


class TestTheLedger:
    def test_no_record_is_no_failure(self, lock_dir):
        assert read_self_upgrade_failure(lock_dir) is None

    def test_a_recorded_failure_reads_back(self, lock_dir):
        record_self_upgrade_failure(
            lock_dir,
            required="0.62.0",
            installed="0.61.0",
            rc=2,
            detail="failed to remove directory `.../lib`: Permission denied",
        )
        rec = read_self_upgrade_failure(lock_dir)
        assert rec is not None
        assert rec.required == "0.62.0"
        assert rec.installed == "0.61.0"
        assert rec.rc == 2
        assert "Permission denied" in rec.detail

    def test_it_is_dot_prefixed_like_its_neighbours(self):
        """``count_held_deployment_locks`` globs ``*.lock`` in this directory.

        ``.draining`` and ``.deferred-restarts`` are dot-prefixed for the same
        reason: a stray file here changes the answer to "is a deploy in flight".
        """
        assert SELF_UPGRADE_FAILURE_FILE.startswith(".")
        assert not SELF_UPGRADE_FAILURE_FILE.endswith(".lock")

    def test_a_later_success_clears_it(self, lock_dir):
        record_self_upgrade_failure(
            lock_dir, required="0.62.0", installed="0.61.0", rc=2, detail="boom"
        )
        clear_self_upgrade_failure(lock_dir)
        assert read_self_upgrade_failure(lock_dir) is None

    def test_clearing_nothing_is_not_an_error(self, lock_dir):
        clear_self_upgrade_failure(lock_dir)
        assert read_self_upgrade_failure(lock_dir) is None

    def test_a_corrupt_record_reads_as_no_record(self, lock_dir):
        """A half-written record must not take `doctor` down with it."""
        (lock_dir / SELF_UPGRADE_FAILURE_FILE).write_text("{not json")
        assert read_self_upgrade_failure(lock_dir) is None

    def test_an_unwritable_lock_dir_does_not_raise(self, tmp_path):
        """Best-effort: recording a failure must never mask the failure itself."""
        record_self_upgrade_failure(
            tmp_path / "does-not-exist",
            required="0.62.0",
            installed="0.61.0",
            rc=2,
            detail="boom",
        )

    def test_a_second_failure_replaces_the_first(self, lock_dir):
        record_self_upgrade_failure(
            lock_dir, required="0.62.0", installed="0.61.0", rc=2, detail="first"
        )
        record_self_upgrade_failure(
            lock_dir, required="0.63.0", installed="0.61.0", rc=9, detail="second"
        )
        rec = read_self_upgrade_failure(lock_dir)
        assert rec is not None
        assert rec.rc == 9
        assert rec.detail == "second"

    def test_the_detail_is_bounded(self, lock_dir):
        """uv's stderr can be long; the ledger sits in a runtime dir."""
        record_self_upgrade_failure(
            lock_dir,
            required="0.62.0",
            installed="0.61.0",
            rc=2,
            detail="x" * 100_000,
        )
        rec = read_self_upgrade_failure(lock_dir)
        assert rec is not None
        assert len(rec.detail) < 10_000

    def test_it_records_when_it_happened(self, lock_dir):
        record_self_upgrade_failure(
            lock_dir, required="0.62.0", installed="0.61.0", rc=2, detail="boom"
        )
        rec = read_self_upgrade_failure(lock_dir)
        assert rec is not None
        assert rec.recorded_at, "an operator needs to correlate this with a journal"


class TestTheDoctorCheck:
    """A debt nothing paid must be visible, per v0.63.0's own invariant."""

    def _run(self, config):
        from fraisier.doctor import DOCTOR_CHECKS

        return DOCTOR_CHECKS["self_upgrade_failure"].fn(config)

    def test_it_is_registered(self):
        from fraisier.doctor import DOCTOR_CHECKS

        assert "self_upgrade_failure" in DOCTOR_CHECKS

    def test_no_config_skips(self):
        assert self._run(None).status == "skip"

    def test_clean_host_passes(self, lock_dir):
        from types import SimpleNamespace

        config = SimpleNamespace(deployment=SimpleNamespace(lock_dir=str(lock_dir)))
        assert self._run(config).status == "pass"

    def test_a_recorded_failure_warns_and_names_the_versions(self, lock_dir):
        from types import SimpleNamespace

        record_self_upgrade_failure(
            lock_dir,
            required="0.62.0",
            installed="0.61.0",
            rc=2,
            detail="Permission denied",
        )
        config = SimpleNamespace(deployment=SimpleNamespace(lock_dir=str(lock_dir)))
        result = self._run(config)
        assert result.status == "warn"
        assert "0.62.0" in result.detail
        assert result.fix_hint

    def test_no_lock_dir_configured_skips(self):
        from types import SimpleNamespace

        config = SimpleNamespace(deployment=SimpleNamespace(lock_dir=None))
        assert self._run(config).status == "skip"


class TestItIsDocumented:
    def test_doctor_md_covers_the_check(self):
        doc = (
            Path(__file__).resolve().parent.parent / "docs" / "doctor.md"
        ).read_text()
        assert "self_upgrade_failure" in doc

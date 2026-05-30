"""Tests for the cross-process lock-dir scanning helpers.

Covers ``count_held_deployment_locks`` (used by the webhook draining check
and the self-upgrade worker's drain loop) and the draining-flag primitives
(``is_draining``, ``clear_draining_flag``, ``DRAINING_FLAG_NAME``).
"""

from __future__ import annotations

import fcntl
import multiprocessing
from pathlib import Path

import pytest

from fraisier.locking import (
    DRAINING_FLAG_NAME,
    clear_draining_flag,
    count_held_deployment_locks,
    file_deployment_lock,
    is_draining,
)


def _hold_lock(lock_dir: Path, name: str, ready_event, release_event) -> None:
    """Hold a file lock until release_event is set. Top-level for spawn pickling."""
    with file_deployment_lock(name, lock_dir=lock_dir):
        ready_event.set()
        release_event.wait(timeout=5)


class TestCountHeldDeploymentLocks:
    """Unit tests for :func:`count_held_deployment_locks`."""

    def test_returns_zero_for_missing_dir(self, tmp_path):
        assert count_held_deployment_locks(tmp_path / "nope") == 0

    def test_returns_zero_for_empty_dir(self, tmp_path):
        assert count_held_deployment_locks(tmp_path) == 0

    def test_returns_zero_when_lock_files_present_but_unheld(self, tmp_path):
        (tmp_path / "a.lock").touch()
        (tmp_path / "b.lock").touch()
        assert count_held_deployment_locks(tmp_path) == 0

    def test_counts_one_held_lock_cross_process(self, tmp_path):
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        release = ctx.Event()
        proc = ctx.Process(
            target=_hold_lock, args=(tmp_path, "myfraise", ready, release)
        )
        proc.start()
        try:
            ready.wait(timeout=5)
            assert count_held_deployment_locks(tmp_path) == 1
        finally:
            release.set()
            proc.join(timeout=5)

    def test_handles_vanishing_files(self, tmp_path, monkeypatch):
        """A lock file that disappears mid-scan is not counted and does not raise."""
        (tmp_path / "ghost.lock").touch()
        real_open = Path.open

        def open_raises_missing(self, *args, **kwargs):
            if self.name == "ghost.lock":
                raise FileNotFoundError(self)
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_raises_missing)
        assert count_held_deployment_locks(tmp_path) == 0

    def test_excludes_draining_flag(self, tmp_path):
        (tmp_path / DRAINING_FLAG_NAME).touch()
        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Event()
        release = ctx.Event()
        proc = ctx.Process(
            target=_hold_lock, args=(tmp_path, "myfraise", ready, release)
        )
        proc.start()
        try:
            ready.wait(timeout=5)
            # Only the *.lock file counts — the hidden flag must not match.
            assert count_held_deployment_locks(tmp_path) == 1
        finally:
            release.set()
            proc.join(timeout=5)

    def test_no_fd_leak_when_flock_raises(self, tmp_path, monkeypatch):
        """A non-BlockingIOError from ``flock`` must still close the fd."""
        (tmp_path / "x.lock").touch()
        original_flock = fcntl.flock
        opened: list = []
        real_open = Path.open

        def tracking_open(self, *args, **kwargs):
            fd = real_open(self, *args, **kwargs)
            opened.append(fd)
            return fd

        def boom(*_a, **_kw):
            raise OSError("I/O error")

        monkeypatch.setattr(Path, "open", tracking_open)
        monkeypatch.setattr("fraisier.locking.fcntl.flock", boom)
        with pytest.raises(OSError, match="I/O error"):
            count_held_deployment_locks(tmp_path)
        # Restore so other tests do not inherit the patch behaviour.
        monkeypatch.setattr("fraisier.locking.fcntl.flock", original_flock)
        assert opened, "expected open() to be called"
        assert all(fd.closed for fd in opened)


class TestDrainingFlag:
    """Unit tests for the ``.draining`` flag primitives."""

    def test_is_draining_false_for_missing_flag(self, tmp_path):
        assert is_draining(tmp_path) is False

    def test_is_draining_true_when_flag_present(self, tmp_path):
        (tmp_path / DRAINING_FLAG_NAME).touch()
        assert is_draining(tmp_path) is True

    def test_clear_draining_flag_removes_existing_flag(self, tmp_path):
        flag = tmp_path / DRAINING_FLAG_NAME
        flag.touch()
        clear_draining_flag(tmp_path)
        assert not flag.exists()

    def test_clear_draining_flag_missing_is_noop(self, tmp_path):
        clear_draining_flag(tmp_path)
        assert not (tmp_path / DRAINING_FLAG_NAME).exists()

    def test_is_draining_false_when_lock_dir_missing(self, tmp_path):
        assert is_draining(tmp_path / "nope") is False

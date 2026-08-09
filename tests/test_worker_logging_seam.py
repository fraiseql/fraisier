"""A detached worker that cannot speak cannot report its own failure (#351).

`maybe_self_upgrade` and `maybe_apply_deferred_restarts` both spawn a
``python -m fraisier.<module>`` worker. Neither worker configured logging, and
with no handler Python falls back to :data:`logging.lastResort` — **WARNING
level, straight to stderr**. So every ``log.info`` was dropped outright,
including the line naming the command about to run, and the deferred-restart
worker fared worse still: it is spawned with stdout *and* stderr on ``DEVNULL``,
so even its warnings were unrecoverable.

The four socket helpers each carried an identical hand-copied ``basicConfig``.
That is the seam these two drifted from, so it gets a name and a guard rather
than a fifth and sixth copy.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from fraisier.worker_logging import WORKER_LOG_FORMAT, configure_worker_logging

_FRAISIER_DIR = Path(__file__).resolve().parent.parent / "fraisier"

#: Matches the argv fragment a spawn site uses: "-m", "fraisier.<module>".
_SPAWNED_MODULE = re.compile(r'"-m",\s*"fraisier\.([a-z_]+)"', re.MULTILINE)


def _modules_spawned_as_workers() -> set[str]:
    """Every ``fraisier.X`` launched via ``python -m`` anywhere in the tree.

    Discovered rather than listed, so a new worker is covered the day it is
    written instead of the day someone remembers this file.
    """
    found: set[str] = set()
    for path in _FRAISIER_DIR.rglob("*.py"):
        found.update(_SPAWNED_MODULE.findall(path.read_text()))
    return found


class TestTheSeamItself:
    def test_it_attaches_a_handler_to_the_root_logger(self):
        """Without a handler, `log.info` falls through to lastResort and dies.

        Asserted on the root logger rather than through ``caplog``, which
        installs a handler of its own and would make this pass even against the
        unconfigured code it exists to catch.
        """
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        try:
            root.handlers.clear()
            configure_worker_logging()
            assert root.handlers, (
                "no root handler: every log.info in the worker would be dropped "
                "by logging.lastResort, which is WARNING-level"
            )
            assert root.level <= logging.INFO
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

    def test_it_is_idempotent(self):
        """`basicConfig` is a no-op once configured; calling twice must not raise."""
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        try:
            root.handlers.clear()
            configure_worker_logging()
            configure_worker_logging()
            assert len(root.handlers) == 1
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

    def test_it_keeps_the_format_the_helpers_already_used(self):
        """The four socket helpers' format, so journal output does not change."""
        assert WORKER_LOG_FORMAT == "%(levelname)s %(name)s: %(message)s"


class TestEveryDetachedWorkerRoutesThroughIt:
    """The guard. A worker that skips the seam is the #351 defect returning."""

    def test_the_scan_finds_the_workers_we_know_about(self):
        """Meta-test: a tree scan that matches nothing looks exactly like a pass.

        v0.61.0's rule. Without this, deleting the spawn sites — or breaking the
        regex — would turn the guard below green rather than red.
        """
        found = _modules_spawned_as_workers()
        assert {"webhook_self_upgrade", "deferred_restart"} <= found, (
            f"the spawn-site scan is broken or the workers moved; found={found}"
        )

    @pytest.mark.parametrize("module", sorted(_modules_spawned_as_workers()))
    def test_worker_configures_logging_before_it_works(self, module):
        source = (_FRAISIER_DIR / f"{module}.py").read_text()
        assert "configure_worker_logging()" in source, (
            f"fraisier.{module} is spawned as a detached worker but never calls "
            "configure_worker_logging(); its log.info output would be discarded "
            "by logging.lastResort"
        )

    def test_the_guard_can_fail(self, tmp_path, monkeypatch):
        """Meta-test: prove the assertion above is capable of going red."""
        fake = tmp_path / "fraisier"
        fake.mkdir()
        (fake / "silent_worker.py").write_text(
            'import logging\nlog = logging.getLogger(__name__)\n"-m", "x"\n'
        )
        source = (fake / "silent_worker.py").read_text()
        assert "configure_worker_logging()" not in source


class TestTheSpawnSiteKeepsTheOutput:
    """Configuring logging is pointless if the spawn throws it away."""

    @staticmethod
    def _spawn(tmp_path, monkeypatch, *, log_dir):
        from unittest.mock import patch

        from fraisier.deferred_restart import (
            DEFERRED_RESTART_FILE,
            maybe_apply_deferred_restarts,
        )

        monkeypatch.setattr("fraisier.deferred_restart._LOG_DIR", log_dir)
        lock_dir = tmp_path / "run-fraisier"
        lock_dir.mkdir()
        (lock_dir / DEFERRED_RESTART_FILE).write_text("a.service\n")
        seen: dict = {}

        class _FakeProc:
            pid = 4321

        def fake_popen(cmd, **kwargs):
            seen.update(kwargs)
            return _FakeProc()

        with patch("fraisier.deferred_restart.subprocess.Popen", fake_popen):
            maybe_apply_deferred_restarts(
                lock_dir=lock_dir, socket_path="/run/fraisier/systemctl.sock"
            )
        return seen

    def test_deferred_restart_worker_output_is_not_discarded(
        self, tmp_path, monkeypatch
    ):
        import subprocess as sp

        seen = self._spawn(tmp_path, monkeypatch, log_dir=tmp_path / "logs")

        assert seen, "the deferred-restart worker was never spawned"
        assert seen.get("stdout") is not sp.DEVNULL, (
            "the worker's diagnostics are discarded — it is the only thing that "
            "can say *why* a deferred restart went unpaid"
        )
        assert seen.get("stderr") == sp.STDOUT, "stderr must join the same log"
        assert (tmp_path / "logs").is_dir()

    def test_an_unwritable_log_dir_still_spawns_the_worker(self, tmp_path, monkeypatch):
        """Losing the log is bad; leaving the debt unpaid *and* unexplained is worse.

        This is the fallback the self-upgrade worker already had, and it is why
        the test above must point `_LOG_DIR` somewhere writable — otherwise it
        passes against DEVNULL for an unrelated reason.
        """
        import subprocess as sp

        seen = self._spawn(tmp_path, monkeypatch, log_dir=Path("/dev/null/nope"))

        assert seen, "an unwritable log dir must not stop the worker"
        assert seen.get("stdout") is sp.DEVNULL

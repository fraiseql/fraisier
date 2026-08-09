"""One place a subprocess entrypoint configures logging (#351).

fraisier spawns two kinds of child process that log for themselves: the four
socket helpers, which systemd starts as their own units, and the two detached
``python -m fraisier.<module>`` workers that outlive the process which spawned
them.

The helpers each carried an identical hand-copied ``basicConfig``. The two
workers had none — and with no handler configured, Python falls back to
:data:`logging.lastResort`, which is **WARNING level and writes to stderr**. So
every ``log.info`` in a worker was dropped before it reached anything, including
the line naming the command it was about to run. A self-upgrade that destroyed
its own venv left a 200-byte log holding the failure and no record of what
produced it.

Copying the call a fifth and sixth time would have worked and drifted again, so
it gets a name here and a guard in ``tests/test_worker_logging_seam.py``: every
module discovered as a ``-m`` spawn target must route through this function.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

log = logging.getLogger(__name__)

#: The format the four socket helpers already emitted. Kept byte-identical so
#: adopting this seam does not change how any existing unit reads in the journal.
WORKER_LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"


def configure_worker_logging(level: int = logging.INFO) -> None:
    """Attach a root handler so a child process's own INFO output survives.

    Idempotent, because :func:`logging.basicConfig` returns without doing
    anything once the root logger has a handler — which is what makes this safe
    to call from a ``_main`` that a test may invoke more than once.
    """
    logging.basicConfig(level=level, format=WORKER_LOG_FORMAT)


def open_worker_log(log_dir: Path, stem: str):
    """Open an append-mode file to hold a detached worker's stdout and stderr.

    Configuring a handler is only half the job: a worker spawned with its output
    on ``DEVNULL`` is exactly as silent as one with no handler at all. Callers
    pass their own directory rather than reading a shared constant, so each
    module keeps the module-level path its tests already monkeypatch.

    Falls back to ``subprocess.DEVNULL`` when the directory cannot be opened —
    losing the log is bad, but refusing to spawn the worker would leave the debt
    unpaid *and* unexplained.
    """
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        return (log_dir / f"{stem}-{ts}.log").open("ab")
    except OSError as exc:
        log.warning(
            "could not open a worker log under %s (%s); using DEVNULL", log_dir, exc
        )
        return subprocess.DEVNULL

"""Pay the restarts ``install.sh`` deferred because a deploy was in flight (#349).

``install.sh`` may not restart a unit that hosts a deploy — doing so kills the
deploy that asked for the install. So it records what it did not restart in
``{lock_dir}/.deferred-restarts`` and leaves the units installed, daemon-reloaded
and still running their previous version.

That state is a debt, not a fix. A webhook running its old unit carries the old
``ReadWritePaths=`` and ``Environment=``, so a ``fraises.yaml`` change that adds
an environment leaves it unable to write the new ``app_path`` and the *next*
deploy fails on a read-only filesystem. The debt is paid here: once the deploy
releases its lock, a detached worker drains and sends the restart over the
systemctl-helper socket, reusing the sequence :mod:`fraisier.drain_restart`
already performs for the self-upgrade restart.

**A ledger entry is cleared only when its restart succeeded.** A unit the helper
refuses — deploy sockets are not in its allowlist — stays recorded, so
``fraisier doctor`` keeps reporting a unit that is installed and not running.

Scope, stated: the worker is spawned from the webhook's deploy path, where it
outlives the process that spawned it. A deploy run by the socket-activated
``deploy-daemon`` exits with its per-connection service instance, and systemd
kills that instance's cgroup — so a worker spawned there may not survive its own
drain. The ledger is what covers that: it is not cleared, and ``doctor`` reports
it until an operator or a later webhook deploy pays it.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from fraisier.drain_restart import (
    DEFAULT_DRAIN_POLL_S,
    DEFAULT_DRAIN_SETTLE_S,
    DEFAULT_DRAIN_TIMEOUT_S,
    DEFAULT_LOCK_DIR,
    DRAIN_TIMEOUT_RC,
    draining_flag,
    send_restart,
    wait_for_deploys_to_drain,
)
from fraisier.worker_logging import configure_worker_logging, open_worker_log

log = logging.getLogger(__name__)

#: Beside the self-upgrade worker's logs, for the same reason: this worker is
#: detached and outlives its parent, so its stderr has nowhere else to go.
#: It used to be spawned onto DEVNULL, which discarded the only explanation of
#: *why* a deferred restart went unpaid.
_LOG_DIR = Path("/var/lib/fraisier/deferred-restart")

#: Lives beside ``.draining`` in the lock dir, and dot-prefixed for the same
#: reason: ``count_held_deployment_locks`` globs ``*.lock`` and must not see it.
DEFERRED_RESTART_FILE = ".deferred-restarts"

#: No RPC channel, so the debt could not even be attempted.
NO_SOCKET_RC = 3


def read_deferred_restarts(lock_dir: Path) -> list[str]:
    """Units ``install.sh`` installed but did not restart, in recorded order."""
    path = Path(lock_dir) / DEFERRED_RESTART_FILE
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return []
    except OSError as exc:
        # A debt we cannot read must not break the deploy that just succeeded.
        # `doctor` reads the same file and reports the failure in its own right.
        log.warning("could not read deferred restarts at %s: %s", path, exc)
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def settle_deferred_restarts(lock_dir: Path, *, paid: list[str]) -> None:
    """Drop the units whose restart succeeded; keep everything else on the books."""
    path = Path(lock_dir) / DEFERRED_RESTART_FILE
    remaining = [u for u in read_deferred_restarts(lock_dir) if u not in set(paid)]
    try:
        if not remaining:
            path.unlink(missing_ok=True)
            return
        path.write_text("".join(f"{u}\n" for u in remaining))
    except OSError as exc:
        log.warning("could not update deferred restarts at %s: %s", path, exc)


def run_deferred_restarts(
    socket_path: str,
    *,
    lock_dir: Path,
    drain_timeout_s: int = DEFAULT_DRAIN_TIMEOUT_S,
    drain_poll_s: float = DEFAULT_DRAIN_POLL_S,
    drain_settle_s: float = DEFAULT_DRAIN_SETTLE_S,
) -> int:
    """Drain, then restart every unit on the ledger. Returns a worker exit code."""
    pending = read_deferred_restarts(lock_dir)
    if not pending:
        return 0
    if not socket_path:
        log.warning(
            "deferred-restart: FRAISIER_SYSTEMCTL_SOCKET not set; %s still needs "
            "a restart. Run: sudo systemctl restart %s",
            ", ".join(pending),
            " ".join(pending),
        )
        return NO_SOCKET_RC

    # The flag covers settle + drain so dispatch refuses new deploys for the
    # whole window, exactly as the self-upgrade worker does.
    with draining_flag(Path(lock_dir)):
        # Brief settle so a deploy accepted in the dispatch->lock window reaches
        # `with deployment_lock(...)` before we count.
        time.sleep(drain_settle_s)
        result = wait_for_deploys_to_drain(
            Path(lock_dir), drain_timeout_s, drain_poll_s
        )
        if not result.drained:
            log.warning(
                "deferred-restart: drain timeout (%ds) — held locks: %s; %s still "
                "needs a restart and stays recorded for `fraisier doctor`.",
                drain_timeout_s,
                ", ".join(result.held),
                ", ".join(pending),
            )
            return DRAIN_TIMEOUT_RC

    paid = [unit for unit in pending if send_restart(socket_path, unit) == 0]
    settle_deferred_restarts(Path(lock_dir), paid=paid)
    unpaid = [unit for unit in pending if unit not in set(paid)]
    if unpaid:
        log.warning(
            "deferred-restart: could not restart %s (the systemctl helper "
            "allowlists services, not sockets). Restart manually: sudo systemctl "
            "restart %s",
            ", ".join(unpaid),
            " ".join(unpaid),
        )
        return 1
    log.info("deferred-restart: restarted %s", ", ".join(paid))
    return 0


def maybe_apply_deferred_restarts(
    *,
    lock_dir: Path,
    socket_path: str,
    drain_timeout_s: int = DEFAULT_DRAIN_TIMEOUT_S,
    drain_poll_s: float = DEFAULT_DRAIN_POLL_S,
    drain_settle_s: float = DEFAULT_DRAIN_SETTLE_S,
) -> None:
    """Best-effort: spawn the detached worker when a debt is recorded.

    Never raises. This runs at the end of a deploy, and a failure to schedule a
    restart must not turn a deploy that succeeded into one that reports failure —
    the ledger survives either way and ``doctor`` reports it.
    """
    try:
        pending = read_deferred_restarts(lock_dir)
        if not pending:
            return
        if not socket_path:
            log.warning(
                "deferred-restart: no systemctl-helper socket configured; %s is "
                "installed but still running its previous version. Restart it "
                "manually, or run `fraisier doctor` to see the pending set.",
                ", ".join(pending),
            )
            return
        log.info(
            "deferred-restart: %s pending; draining before restart", ", ".join(pending)
        )
        cmd = [
            sys.executable,
            "-m",
            "fraisier.deferred_restart",
            "--socket",
            socket_path,
            "--lock-dir",
            str(lock_dir),
            "--drain-timeout",
            str(drain_timeout_s),
            "--drain-poll",
            str(drain_poll_s),
            "--drain-settle",
            str(drain_settle_s),
        ]
        # start_new_session so the worker survives the restart it is about to
        # request, the same reason the self-upgrade worker detaches. Its output
        # goes to a file rather than DEVNULL: the ledger records *that* a debt
        # went unpaid, and only this log records *why* — a drain that timed out
        # reads nothing like a unit the helper refused (#351).
        stdout = open_worker_log(_LOG_DIR, "deferred-restart")
        subprocess.Popen(
            cmd,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
        )
    except Exception:
        log.exception("deferred-restart: could not schedule the pending restarts")


def _main() -> None:  # pragma: no cover - exercised via run_deferred_restarts
    configure_worker_logging()
    parser = argparse.ArgumentParser(prog="fraisier.deferred_restart")
    parser.add_argument(
        "--socket", default=os.environ.get("FRAISIER_SYSTEMCTL_SOCKET", "")
    )
    parser.add_argument("--lock-dir", default=DEFAULT_LOCK_DIR, dest="lock_dir")
    parser.add_argument(
        "--drain-timeout",
        type=int,
        default=DEFAULT_DRAIN_TIMEOUT_S,
        dest="drain_timeout",
    )
    parser.add_argument(
        "--drain-poll", type=float, default=DEFAULT_DRAIN_POLL_S, dest="drain_poll"
    )
    parser.add_argument(
        "--drain-settle",
        type=float,
        default=DEFAULT_DRAIN_SETTLE_S,
        dest="drain_settle",
    )
    args = parser.parse_args()
    sys.exit(
        run_deferred_restarts(
            args.socket,
            lock_dir=Path(args.lock_dir),
            drain_timeout_s=args.drain_timeout,
            drain_poll_s=args.drain_poll,
            drain_settle_s=args.drain_settle,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    _main()

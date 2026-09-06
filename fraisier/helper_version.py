"""Long-running root helpers retire themselves when fraisier is replaced (#391).

Every root helper — systemctl, install, scaffold-install, unit-installer — runs
from the deploy user's venv as a ``Type=simple`` daemon behind an ``Accept=no``
socket: one process gets the listening fd and serves every connection from a
single accept loop. Upgrading fraisier on the host replaces the code on disk and
leaves those processes running the old one, indefinitely. ``maybe_self_upgrade``
restarts the webhook and nothing else.

The helpers notice instead. ``importlib.metadata.version`` re-reads its
dist-info from disk on each call, in a live process, so a helper can compare the
version it started with against the version installed now and exit cleanly when
they differ. Socket activation brings the next connection up on the new code —
which is why exiting is safe here and would not be for an ordinary daemon: the
socket unit keeps listening, and nothing sets ``BindsTo`` or ``Accept``.

Nothing here escalates anything. A helper only ends itself.
"""

from __future__ import annotations

import importlib.metadata
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import socket
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: How long ``accept()`` waits before the loop looks up from the socket. The
#: loop blocks in ``accept()``, so a helper that only checked after serving
#: would keep old code until something called it — and the caller that arrived
#: first would be served by the stale process.
DEFAULT_POLL_INTERVAL_S: float = 30.0


def installed_version() -> str | None:
    """The fraisier version currently installed on disk, or None.

    Read fresh every call: that is the whole mechanism. ``None`` when the
    metadata cannot be read at all — a venv mid-upgrade, a half-removed
    dist-info (#351) — which callers must treat as "no answer", never as a
    change.
    """
    try:
        return importlib.metadata.version("fraisier")
    except Exception:
        # Deliberately broad: this runs in a root daemon's loop, and no
        # failure to answer a diagnostic question may end it.
        logger.debug("Could not read the installed fraisier version", exc_info=True)
        return None


class VersionWatch:
    """Notices when the fraisier under this process has been replaced."""

    def __init__(self) -> None:
        self.started_with = installed_version()
        if self.started_with is None:
            logger.warning(
                "Could not read the installed fraisier version at startup; this "
                "helper will not notice an upgrade and must be restarted by hand "
                "after one."
            )

    def is_stale(self) -> bool:
        """Whether the installed version has moved since this process started.

        False whenever the answer is unknown — no baseline, or no readable
        version now. Staleness detection must not become a new way for a root
        daemon to die.
        """
        if self.started_with is None:
            return False
        current = installed_version()
        if current is None or current == self.started_with:
            return False
        logger.info(
            "fraisier changed under this helper: %s → %s. Exiting so the next "
            "connection starts it on the new code.",
            self.started_with,
            current,
        )
        return True


def serve_until_stale(
    server_sock: socket.socket,
    handle: Callable[[socket.socket], None],
    *,
    is_stale: Callable[[], bool],
    poll_interval: float = DEFAULT_POLL_INTERVAL_S,
) -> None:
    """Accept and serve connections until *is_stale* says to stop.

    Returns — it does not exit the process — so a caller keeps whatever
    shutdown it already had. The staleness check runs both when ``accept()``
    times out (so an idle helper turns over without waiting for a caller) and
    after each request is served (so a busy one does too). Never between
    accepting a connection and answering it: that would drop the request.

    A handler that raises is logged and the loop continues; a
    systemd-supervised socket server must not be brought down by one bad
    request.
    """
    server_sock.settimeout(poll_interval)
    while True:
        if is_stale():
            return
        try:
            conn, _ = server_sock.accept()
        except TimeoutError:
            continue
        except OSError as exc:
            logger.error("accept() failed: %s", exc)
            return
        try:
            handle(conn)
        except Exception as exc:
            # Bare-except is intentional: any handler crash must not bring down
            # the systemd-supervised socket server. logger.exception captures
            # the traceback; binding `exc` surfaces the type/repr in the line.
            logger.exception("Unhandled error in connection handler: %s", exc)

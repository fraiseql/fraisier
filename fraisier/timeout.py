"""Thread-based deployment timeout — safe replacement for SIGALRM.

Unlike SIGALRM, this approach:
- Works in multi-threaded code
- Can trigger cleanup callbacks (e.g. kill subprocess groups)
- Doesn't interfere with asyncio event loops
"""

from __future__ import annotations

import ctypes
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

logger = logging.getLogger(__name__)


class DeploymentTimeoutExpired(Exception):
    """Raised when a deployment exceeds its configured timeout."""


@dataclass
class TimeoutContext:
    """Holds the timer reference so callers can inspect cancellation."""

    timer: threading.Timer


def _interrupt_main_thread(
    on_timeout: Callable[[], None] | None,
) -> None:
    """Raise DeploymentTimeoutExpired in the main thread."""
    if on_timeout is not None:
        on_timeout()

    # Inject exception into the main thread
    main_tid = threading.main_thread().ident
    if main_tid is not None:
        rc = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(main_tid),
            ctypes.py_object(DeploymentTimeoutExpired),
        )
        if rc == 0:
            logger.warning(
                "PyThreadState_SetAsyncExc: thread not found (tid=%s)", main_tid
            )
        elif rc > 1:
            # Multiple threads affected — undo to avoid corruption
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(main_tid), None
            )
            logger.error(
                "PyThreadState_SetAsyncExc affected %d threads, undone", rc
            )


@contextmanager
def deployment_timeout(
    seconds: float,
    on_timeout: Callable[[], None] | None = None,
) -> Generator[TimeoutContext]:
    """Context manager that raises DeploymentTimeoutExpired after *seconds*.

    Args:
        seconds: Maximum time in seconds before timeout fires.
        on_timeout: Optional callback invoked when timeout fires
            (e.g. to kill a subprocess group).  Called from the
            timer thread, before the exception is raised.

    Yields:
        TimeoutContext with a reference to the timer (for inspection).
    """
    timer = threading.Timer(
        seconds,
        _interrupt_main_thread,
        args=(on_timeout,),
    )
    timer.daemon = True
    ctx = TimeoutContext(timer=timer)
    timer.start()
    try:
        yield ctx
    except DeploymentTimeoutExpired:
        raise DeploymentTimeoutExpired(
            f"Deployment timed out after {seconds} seconds"
        ) from None
    finally:
        timer.cancel()
        timer.join(timeout=1.0)

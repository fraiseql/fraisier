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
import time
from contextlib import contextmanager
from contextvars import ContextVar
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
    deadline: float

    def remaining(self) -> float:
        """Seconds left before the timer fires. Never negative."""
        return max(0.0, self.deadline - time.monotonic())


_active_budget: ContextVar[TimeoutContext | None] = ContextVar(
    "fraisier_deployment_budget", default=None
)


def remaining_budget() -> float | None:
    """Seconds left in the innermost active :func:`deployment_timeout`.

    ``None`` when no deploy timer is running — a CLI install, a test. Callers
    that block on something they can bound use it to derive their own limit, so
    no single wait can outlive the deploy that is waiting on it (#384).
    """
    ctx = _active_budget.get()
    return ctx.remaining() if ctx is not None else None


#: Lower bound for a wait derived from the deploy budget. Never 0:
#: ``socket.settimeout(0)`` means *non-blocking*, not "expire immediately", so a
#: nearly-exhausted budget would turn into a spurious ``BlockingIOError`` on the
#: first read. Overshooting an already-expired budget by a second changes
#: nothing that matters.
MIN_DERIVED_TIMEOUT_S: float = 1.0


def derived_timeout(default: float) -> float:
    """A bound for a call that is about to block, in seconds.

    What is left of this deploy's ``timeout:`` budget, so no single wait can
    outlive the deploy waiting on it; *default* when there is no deploy timer,
    as for a CLI-driven install. Never below
    :data:`MIN_DERIVED_TIMEOUT_S`.
    """
    remaining = remaining_budget()
    if remaining is None:
        return float(default)
    return max(MIN_DERIVED_TIMEOUT_S, remaining)


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
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(main_tid), None)
            logger.error("PyThreadState_SetAsyncExc affected %d threads, undone", rc)


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

    What this guarantees, and what it does not: the exception is delivered with
    ``PyThreadState_SetAsyncExc``, which raises at the next bytecode boundary.
    A thread inside a C-level wait — a database driver waiting on a socket, a
    child process being waited for — reaches no boundary until that wait
    returns. So ``timeout:`` is checked **between steps**: each step carries its
    own hard bound where one can be set, and a step that blocks past the budget
    is *reported* when it returns, not interrupted. Use :func:`remaining_budget`
    to derive a bound for anything you are about to block on.
    """
    timer = threading.Timer(
        seconds,
        _interrupt_main_thread,
        args=(on_timeout,),
    )
    timer.daemon = True
    ctx = TimeoutContext(timer=timer, deadline=time.monotonic() + seconds)
    token = _active_budget.set(ctx)
    timer.start()
    try:
        yield ctx
    except DeploymentTimeoutExpired:
        raise DeploymentTimeoutExpired(
            f"Deployment timed out after {seconds} seconds"
        ) from None
    finally:
        _active_budget.reset(token)
        timer.cancel()
        timer.join(timeout=1.0)

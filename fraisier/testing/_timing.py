"""Timing helpers for test database lifecycle phases."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import logging
    from collections.abc import Generator


@dataclass
class Elapsed:
    """Mutable container for elapsed time, set after context manager exits."""

    ms: int = 0


@contextmanager
def timed_phase(
    phase_name: str, logger: logging.Logger
) -> Generator[Elapsed, None, None]:
    """Context manager that logs and records elapsed time for a phase."""
    elapsed = Elapsed()
    start = time.monotonic()
    try:
        yield elapsed
    finally:
        elapsed.ms = int((time.monotonic() - start) * 1000)
        logger.info("%s completed in %d ms", phase_name, elapsed.ms)


@dataclass
class TimingReport:
    """Accumulates phase timings for observability."""

    phases: dict[str, int] = field(default_factory=dict)

    @property
    def total_ms(self) -> int:
        return sum(self.phases.values())

    def record(self, phase: str, duration_ms: int) -> None:
        self.phases[phase] = duration_ms

    def summary(self) -> str:
        lines = [f"  {name}: {ms} ms" for name, ms in self.phases.items()]
        lines.append(f"  total: {self.total_ms} ms")
        return "\n".join(lines)

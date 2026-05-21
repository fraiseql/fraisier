"""Deployment duration estimator (issue #201).

Looks up the most recent successful deploys for
``(fraise, environment, strategy)`` and returns a median-based estimate.
Falls back to a strategy-specific per-MB rate (or a per-strategy floor when
the database size is unknown) when fewer than three samples are available.

Used by the webhook response and the CLI deploy commands to give human and
agentic callers a "wait ~Nm" signal at trigger time.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover
    from fraisier.database import FraisierDB

log = logging.getLogger(__name__)

# Median-buffer factor applied to history-based estimates.
_HISTORY_BUFFER = 1.20

# Minimum number of historical samples required to trust the median.
_MIN_HISTORY_SAMPLES = 3

# History lookback size.
_HISTORY_LIMIT = 5

# Per-strategy seconds-per-MB used for the fallback estimator. Approximate
# orders of magnitude; the estimator clamps to STRATEGY_FLOOR_SECONDS so a
# small database does not produce an unrealistically short estimate.
STRATEGY_FALLBACK_SECONDS_PER_MB: dict[str, float] = {
    "rebuild": 0.0025,  # ~400 MB/s rebuild
    "restore_migrate": 0.05,  # ~20 MB/s pg_restore
    "migrate": 5.0,  # roughly per-migration; db_size ignored downstream
}

# Per-strategy floor when db_size is unknown or scaling lands below it.
STRATEGY_FLOOR_SECONDS: dict[str, int] = {
    "rebuild": 180,
    "restore_migrate": 120,
    "migrate": 30,
}

# Generic floor when the strategy is unknown.
_UNKNOWN_STRATEGY_FLOOR = 60


@dataclass(frozen=True)
class EstimateResult:
    """Result of a duration estimate."""

    seconds: int
    confidence: Literal["history", "fallback"]
    samples_used: int


def _fallback_seconds(strategy: str, db_size_mb: int | None) -> int:
    """Return the fallback estimate in seconds for *strategy* and *db_size_mb*."""
    floor = STRATEGY_FLOOR_SECONDS.get(strategy, _UNKNOWN_STRATEGY_FLOOR)
    rate = STRATEGY_FALLBACK_SECONDS_PER_MB.get(strategy)
    if db_size_mb is None or rate is None:
        return floor
    scaled = int(db_size_mb * rate)
    return max(scaled, floor)


def estimate_duration(
    db: FraisierDB,
    *,
    fraise: str,
    environment: str,
    strategy: str,
    db_size_mb: int | None,
) -> EstimateResult:
    """Return an estimate for the next ``(fraise, environment, strategy)`` deploy.

    Returns the median of the most recent up to ``_HISTORY_LIMIT`` successful
    durations, multiplied by ``_HISTORY_BUFFER`` (default 1.20), when at least
    ``_MIN_HISTORY_SAMPLES`` (default 3) samples are available. Otherwise falls
    back to ``STRATEGY_FALLBACK_SECONDS_PER_MB[strategy] * db_size_mb``, clamped
    to ``STRATEGY_FLOOR_SECONDS[strategy]``. Never raises — a DB error returns
    the fallback estimate so a flaky history store cannot block a deploy.
    """
    try:
        durations = db.get_successful_deploy_durations(
            fraise=fraise,
            environment=environment,
            strategy=strategy,
            limit=_HISTORY_LIMIT,
        )
    except Exception:
        log.exception(
            "estimate_duration: history lookup failed for %s/%s/%s — using fallback",
            fraise,
            environment,
            strategy,
        )
        return EstimateResult(
            seconds=_fallback_seconds(strategy, db_size_mb),
            confidence="fallback",
            samples_used=0,
        )

    sample_count = len(durations)
    if sample_count >= _MIN_HISTORY_SAMPLES:
        median = statistics.median(durations)
        return EstimateResult(
            seconds=int(median * _HISTORY_BUFFER),
            confidence="history",
            samples_used=sample_count,
        )
    return EstimateResult(
        seconds=_fallback_seconds(strategy, db_size_mb),
        confidence="fallback",
        samples_used=sample_count,
    )

"""Pure layers behind ``fraisier scheduled-install``.

This module is consumed by ``fraisier/cli/scheduled_install.py``. It owns:

- ``enumerate_scheduled_units``: walks an already-loaded ``FraisierConfig`` and
  yields one ``ScheduledUnitInstall`` per ``systemd_service`` / ``systemd_timer``
  declared on ``type: scheduled`` fraises' ``jobs.*``.

The source-path convention is ``<env.app_path>/scripts/systemd/<unit_name>`` —
the consumer's hand-authored unit files, NOT ``scripts/generated/systemd/``
(which is fraisier's own scaffold output for webhook / install-helper units).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from fraisier.dbops._validation import validate_service_name

if TYPE_CHECKING:
    from fraisier.config import FraisierConfig

SYSTEMD_DEST_DIR = Path("/etc/systemd/system")
APP_PATH_UNITS_SUBDIR = Path("scripts/systemd")


@dataclass(frozen=True)
class ScheduledUnitInstall:
    """One systemd unit (service or timer) declared by a ``type: scheduled`` job."""

    fraise_name: str
    environment: str
    job_name: str
    unit_name: str
    is_timer: bool
    source_path: Path
    dest_path: Path


class UnitState(StrEnum):
    """Per-unit reconciliation state determined by ``classify_unit``."""

    ABSENT = "absent"  # dest does not exist; source does
    IDENTICAL = "identical"  # dest exists, byte-equal to source
    DRIFTED = "drifted"  # dest exists, differs from source
    MISSING_SOURCE = "missing"  # source does not exist (operator error)


@dataclass(frozen=True)
class UnitDiff:
    """Result of classifying one ``ScheduledUnitInstall`` against the filesystem."""

    install: ScheduledUnitInstall
    state: UnitState
    diff_summary: str | None  # short one-line summary for DRIFTED; None otherwise


def enumerate_scheduled_units(
    config: FraisierConfig, environment: str
) -> list[ScheduledUnitInstall]:
    """Return the unit-install rows for ``environment`` across all scheduled fraises."""
    units: list[ScheduledUnitInstall] = []
    for fraise_name, fraise in config.fraises.items():
        if fraise.get("type") != "scheduled":
            continue
        env_config = (fraise.get("environments") or {}).get(environment)
        if env_config is None:
            continue
        app_path = Path(env_config["app_path"])
        for job_name, job in (env_config.get("jobs") or {}).items():
            for field, is_timer in (
                ("systemd_service", False),
                ("systemd_timer", True),
            ):
                unit_name = job.get(field)
                if not unit_name:
                    continue
                validate_service_name(unit_name)
                units.append(
                    ScheduledUnitInstall(
                        fraise_name=fraise_name,
                        environment=environment,
                        job_name=job_name,
                        unit_name=unit_name,
                        is_timer=is_timer,
                        source_path=app_path / APP_PATH_UNITS_SUBDIR / unit_name,
                        dest_path=SYSTEMD_DEST_DIR / unit_name,
                    )
                )
    return units


def classify_unit(install: ScheduledUnitInstall) -> UnitDiff:
    """Compare ``install.source_path`` against ``install.dest_path``; no writes.

    Returns:
        - ``MISSING_SOURCE`` if ``source_path`` does not exist.
        - ``ABSENT`` if ``dest_path`` does not exist (source does).
        - ``IDENTICAL`` if both exist and are byte-equal.
        - ``DRIFTED`` if both exist and differ — with a short one-line summary.
    """
    if not install.source_path.exists():
        return UnitDiff(install, UnitState.MISSING_SOURCE, None)
    if not install.dest_path.exists():
        return UnitDiff(install, UnitState.ABSENT, None)
    src_bytes = install.source_path.read_bytes()
    dst_bytes = install.dest_path.read_bytes()
    if src_bytes == dst_bytes:
        return UnitDiff(install, UnitState.IDENTICAL, None)
    summary = _short_diff_summary(src_bytes, dst_bytes)
    return UnitDiff(install, UnitState.DRIFTED, summary)


def _short_diff_summary(src: bytes, dst: bytes) -> str:
    """One-line summary of byte differences between source and dest unit files.

    Counts added / removed lines from a unified diff (dest → source perspective,
    so "added" means lines present in source but not dest). The full unified
    diff is only emitted by the CLI under ``--verbose`` — keep the summary tight.
    """
    src_lines = src.decode("utf-8", errors="replace").splitlines()
    dst_lines = dst.decode("utf-8", errors="replace").splitlines()
    diff = list(difflib.unified_diff(dst_lines, src_lines, lineterm=""))
    added = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
    return f"unit body differs ({added} lines added, {removed} removed)"

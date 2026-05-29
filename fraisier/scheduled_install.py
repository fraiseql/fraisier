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

from dataclasses import dataclass
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

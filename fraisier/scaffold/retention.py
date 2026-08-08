"""What a host's declared retention policy looks like on disk (#339).

The incident this closes is a corpus that grew until the disk filled
because the unit meant to prune it was hand-written in the consuming
repo, never installed on the destination, and checked by nothing. The
units are fraisier's now, so ``scaffold-diff`` reports a missing one for
free — it derives from the artifact manifest. This module answers the
question ``doctor`` asks instead, which is the operator's: *which corpora
does this host say it keeps, and is anything actually pruning them?*
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fraisier.naming import retention_unit_names
from fraisier.scaffold.artifacts import SYSTEMD_DIR

if TYPE_CHECKING:
    from fraisier.scaffold.renderer import ScaffoldRenderer


@dataclass(frozen=True)
class RetentionStatus:
    """One declared corpus, and whether its units reached the host."""

    name: str
    environment: str
    dir: str
    schedule: str
    service_unit: str
    timer_unit: str
    service_installed: bool
    timer_installed: bool

    @property
    def installed(self) -> bool:
        """Both halves are present.

        Both, because a timer without its service fires into nothing and a
        service without its timer never fires — and either one alone reads
        as "retention is configured" to anyone glancing at the directory.
        """
        return self.service_installed and self.timer_installed

    @property
    def detail(self) -> str:
        state = "installed" if self.installed else "NOT INSTALLED"
        return (
            f"{self.name} ({self.environment}): {self.dir} "
            f"every {self.schedule} — {state}"
        )


def retention_report(
    renderer: ScaffoldRenderer,
    *,
    systemd_dir: Path | str = SYSTEMD_DIR,
) -> list[RetentionStatus]:
    """Status of every retention entry this host declares.

    Reads the entries from the same accessor the renderer writes units
    from, and the unit names from the same authority the renderer and the
    manifest read. Re-deriving either here would be a third writer for a
    fact that has one — which is what #337 was filed for.

    Args:
        renderer: The renderer for this host, read for its local entries
            and project name.
        systemd_dir: Where units live. Overridable so a test can point it
            at a directory it controls rather than the real one.

    Returns:
        One entry per declared corpus, in config order. Empty for a config
        with no ``retain:`` block, which is every config before #339.
    """
    root = Path(systemd_dir)
    project = renderer.context["project_name"]

    report: list[RetentionStatus] = []
    for entry in renderer.retention_entries():
        service_unit, timer_unit = retention_unit_names(
            project, entry.environment, entry.name
        )
        report.append(
            RetentionStatus(
                name=entry.name,
                environment=entry.environment,
                dir=entry.dir,
                schedule=entry.schedule,
                service_unit=service_unit,
                timer_unit=timer_unit,
                service_installed=(root / service_unit).exists(),
                timer_installed=(root / timer_unit).exists(),
            )
        )
    return report

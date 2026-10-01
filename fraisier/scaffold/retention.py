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

from fraisier import naming
from fraisier.naming import pre_migrate_prune_unit_names, retention_unit_names
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
    systemd_dir: Path | str | None = None,
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
    root = Path(SYSTEMD_DIR if systemd_dir is None else systemd_dir)
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


def pre_migrate_prune_report(
    renderer: ScaffoldRenderer,
    *,
    systemd_dir: Path | str | None = None,
) -> list[RetentionStatus]:
    """Status of every ``pre_migrate_dump`` gate this host can prune (#420).

    Reported in the same shape as a received corpus, because it answers the same
    question — *is anything pruning what this host keeps?* — and the same remedy
    applies: the timer exists on a host only after ``scaffold-install``, so an
    upgrade that adds it changes nothing until someone installs it.

    Entries and unit names come from the renderer's own accessor and the same
    naming authority it writes units with.
    """
    root = Path(SYSTEMD_DIR if systemd_dir is None else systemd_dir)
    project = renderer.context["project_name"]

    report: list[RetentionStatus] = []
    for entry in renderer.pre_migrate_prune_entries():
        service_unit, timer_unit = pre_migrate_prune_unit_names(
            project, entry.fraise, entry.environment
        )
        report.append(
            RetentionStatus(
                name=f"pre_migrate_dump:{entry.fraise}",
                environment=entry.environment,
                dir=entry.output_dir,
                schedule=entry.schedule,
                service_unit=service_unit,
                timer_unit=timer_unit,
                service_installed=(root / service_unit).exists(),
                timer_installed=(root / timer_unit).exists(),
            )
        )
    return report


@dataclass(frozen=True)
class PgBackRestHelperStatus:
    """One environment's pgBackRest helper, and whether it is reachable."""

    fraise: str
    environment: str
    service_installed: bool
    socket_installed: bool
    listening: bool

    @property
    def installed(self) -> bool:
        return self.service_installed and self.socket_installed

    @property
    def scope(self) -> str:
        return f"{self.fraise}/{self.environment}"


def pgbackrest_helper_report(
    renderer: ScaffoldRenderer,
    *,
    systemd_dir: Path | str | None = None,
) -> list[PgBackRestHelperStatus]:
    """Status of every pgBackRest helper this host's config says it needs (#424).

    *Installed* (both unit files on disk) and *listening* (the socket the client
    connects to exists) are reported apart: the first is `scaffold-install`'s
    remedy and the second is a unit that is installed and not running.  The socket
    path comes from the naming authority the unit's ``ListenStream=`` and the
    restore source read too, looked up at call time.
    """
    root = Path(SYSTEMD_DIR if systemd_dir is None else systemd_dir)
    project = renderer.context["project_name"]

    report: list[PgBackRestHelperStatus] = []
    for entry in renderer.pgbackrest_helper_entries():
        socket_unit, service_unit = naming.pgbackrest_helper_unit_names(
            project, entry.fraise, entry.environment
        )
        report.append(
            PgBackRestHelperStatus(
                fraise=entry.fraise,
                environment=entry.environment,
                service_installed=(root / service_unit).exists(),
                socket_installed=(root / socket_unit).exists(),
                listening=Path(
                    naming.pgbackrest_helper_socket_path(
                        project, entry.fraise, entry.environment
                    )
                ).exists(),
            )
        )
    return report

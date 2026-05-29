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
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from fraisier.dbops._validation import validate_service_name

if TYPE_CHECKING:
    from fraisier.config import FraisierConfig
    from fraisier.runners import CommandRunner

SYSTEMD_DEST_DIR = Path("/etc/systemd/system")
APP_PATH_UNITS_SUBDIR = Path("scripts/systemd")


class ScheduledInstallError(Exception):
    """Raised when ``apply_unit_diffs`` refuses to converge.

    Covers MISSING_SOURCE, DRIFTED-without-``force``, and path-traversal
    violations. Always raised *before* any filesystem mutation.
    """


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
    app_path: Path  # consumer's app_path; Phase 03 uses it for source-containment check


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
                        app_path=app_path,
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


@dataclass(frozen=True)
class ApplyReport:
    """Summary of what ``apply_unit_diffs`` actually did during one call."""

    written: tuple[ScheduledUnitInstall, ...]  # ABSENT + (DRIFTED with force)
    skipped_identical: tuple[ScheduledUnitInstall, ...]
    enabled_timers: tuple[ScheduledUnitInstall, ...]
    reloaded: bool


def _validate_unit_path_safety(
    install: ScheduledUnitInstall,
    *,
    systemd_dest_dir: Path,
) -> None:
    """Raise if the unit name, dest path, or source path could escape sandboxes.

    Three guards, all evaluated before any filesystem mutation:

    1. ``unit_name`` must not contain ``/`` or ``..``. ``validate_service_name``'s
       regex ``^[a-zA-Z0-9_@.\\-]+$`` rejects ``/`` but accepts ``..`` (any run
       of dots passes), so we add the explicit substring check here.
    2. ``dest_path.parent`` must resolve to ``systemd_dest_dir`` — catches a
       caller that constructed a ``ScheduledUnitInstall`` with a tampered
       ``dest_path``.
    3. ``source_path`` (resolved through any symlinks) must be contained under
       ``app_path/scripts/systemd``. Blocks a hostile worktree from symlinking
       e.g. ``scripts/systemd/foo.timer`` to ``/etc/passwd``.
    """
    if "/" in install.unit_name or ".." in install.unit_name:
        msg = f"unsafe unit name {install.unit_name!r}: contains '/' or '..'"
        raise ScheduledInstallError(msg)

    expected_dest_parent = systemd_dest_dir.resolve()
    actual_dest_parent = install.dest_path.parent.resolve()
    if actual_dest_parent != expected_dest_parent:
        msg = f"dest path {install.dest_path} escapes systemd dir {systemd_dest_dir}"
        raise ScheduledInstallError(msg)

    source_root = (install.app_path / APP_PATH_UNITS_SUBDIR).resolve()
    if not install.source_path.exists():
        # No symlink to resolve; MISSING_SOURCE will fire separately.
        return
    actual_source = install.source_path.resolve()
    if not actual_source.is_relative_to(source_root):
        msg = (
            f"source path {install.source_path} (resolves to {actual_source}) "
            f"escapes {source_root}"
        )
        raise ScheduledInstallError(msg)


def apply_unit_diffs(
    diffs: list[UnitDiff],
    *,
    runner: CommandRunner,
    force: bool = False,
    systemd_dest_dir: Path | None = None,
) -> ApplyReport:
    """Converge the dest filesystem with the source. The only write-the-FS path.

    Order of operations — all guards raise *before* any mutation:

    1. Path-traversal checks on every diff (rejects ``/``, ``..``, dest tamper,
       symlink-escape on source).
    2. ``MISSING_SOURCE`` → raise ``ScheduledInstallError``.
    3. ``DRIFTED`` without ``force=True`` → raise ``ScheduledInstallError``.
    4. Copy each ``ABSENT`` (and each ``DRIFTED`` if ``force``) source → dest,
       chmod 0o644.
    5. If any writes happened, run ``systemctl daemon-reload`` once.
    6. For each timer that was written, run ``systemctl enable --now <unit>``.
       (``IDENTICAL`` timers are NOT re-enabled — that would defeat the
       zero-side-effect re-run invariant.)

    Note on ``sudo``: this function does NOT prefix systemctl invocations with
    ``sudo``. The privilege model (Open Question #2) is that the operator
    invokes the whole command via ``sudo fraisier scheduled-install``, so the
    Python process is already root by the time we get here.
    """
    dest_dir = systemd_dest_dir if systemd_dest_dir is not None else SYSTEMD_DEST_DIR

    for diff in diffs:
        _validate_unit_path_safety(diff.install, systemd_dest_dir=dest_dir)

    missing = [d for d in diffs if d.state is UnitState.MISSING_SOURCE]
    if missing:
        names = ", ".join(d.install.unit_name for d in missing)
        msg = (
            f"source not found for: {names}. "
            "Did the deploy land in app_path/scripts/systemd/?"
        )
        raise ScheduledInstallError(msg)

    if not force:
        drifted = [d for d in diffs if d.state is UnitState.DRIFTED]
        if drifted:
            names = ", ".join(d.install.unit_name for d in drifted)
            msg = (
                f"drifted units (pass --force to overwrite): {names}. "
                "These dest files differ from source; an operator likely "
                "hand-edited them."
            )
            raise ScheduledInstallError(msg)

    written: list[ScheduledUnitInstall] = []
    skipped_identical: list[ScheduledUnitInstall] = []
    for diff in diffs:
        if diff.state is UnitState.ABSENT or (
            diff.state is UnitState.DRIFTED and force
        ):
            shutil.copy2(diff.install.source_path, diff.install.dest_path)
            diff.install.dest_path.chmod(0o644)
            written.append(diff.install)
        elif diff.state is UnitState.IDENTICAL:
            skipped_identical.append(diff.install)

    reloaded = False
    if written:
        runner.run(["systemctl", "daemon-reload"])
        reloaded = True

    enabled: list[ScheduledUnitInstall] = []
    for install in written:
        if install.is_timer:
            runner.run(["systemctl", "enable", "--now", install.unit_name])
            enabled.append(install)

    return ApplyReport(
        written=tuple(written),
        skipped_identical=tuple(skipped_identical),
        enabled_timers=tuple(enabled),
        reloaded=reloaded,
    )

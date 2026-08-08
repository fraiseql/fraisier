"""Backup runner: pg_dump with compression, retention, and scheduling.

Supports full and slim backup modes, disk space checks, retention
cleanup, and per-destination schedule matching.
"""

import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fraisier.dbops._validation import validate_file_path, validate_pg_identifier
from fraisier.dbops.archive import ArchiveVerdict, verify_archive
from fraisier.dbops.operations import _pg_cmd

log = logging.getLogger(__name__)

_COMPRESSION_RE = re.compile(r"^(zstd|lz4|gzip|none)(:\d+)?$")

# A new backup whose size is below this fraction of the previous same-mode
# dump is rejected as implausibly small. Tuned to catch truncation (the
# #202 incident) while tolerating legitimate fluctuation between runs.
_SIZE_SANITY_RATIO = 0.5


def _validate_compression(value: str) -> None:
    """Validate pg_dump compression spec."""
    if not _COMPRESSION_RE.match(value):
        msg = (
            f"Invalid compression: {value!r} — "
            "must match (zstd|lz4|gzip|none)[:<level>]"
        )
        raise ValueError(msg)


def _verify_backup_toc(
    backup_path: str,
    *,
    connection_url: str,  # noqa: ARG001  # Reason: kept for call-site stability
) -> tuple[bool, str]:
    """Verify a just-written dump, in ``run_backup``'s (ok, error) shape.

    Adapter over :func:`fraisier.dbops.archive.verify_archive`, which is the
    one implementation of "is this a readable archive" — the restore and prune
    paths read the same seam (#342). *connection_url* is accepted and unused:
    ``pg_restore --list`` never connects, and removing the parameter would
    churn a working call site for nothing.

    An ``UNVERIFIABLE`` result does **not** fail the backup. The dump was
    written by the ``pg_dump`` that just succeeded, and a host missing the
    client tools should not have its backup condemned by the absence of the
    thing that would have cleared it — it previously raised
    ``FileNotFoundError`` straight out of ``run_backup``. It is logged, so the
    dump does not go out unverified *and* unmentioned.

    Returns (True, "") when the dump is not known-bad, (False, detail) when it is.
    """
    check = verify_archive(backup_path)
    if check.is_bad:
        return False, check.detail
    if check.verdict is ArchiveVerdict.UNVERIFIABLE:
        log.warning("Backup %s could not be verified: %s", backup_path, check.detail)
    return True, ""


def _dump_size(path: Path) -> int:
    """Return the on-disk byte total of a dump file or directory dump.

    For a regular file dump (``pg_dump -Fc``), returns the file size.
    For a directory dump (``pg_dump -Fd``), returns the recursive sum of
    every contained file (``toc.dat`` plus every ``*.dat`` blob). Using
    the directory's own ``stat().st_size`` would return the inode's
    metadata size (typically 4096 bytes), which makes the size sanity
    check meaningless for ``-Fd`` dumps.
    """
    if path.is_dir():
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return path.stat().st_size


def _previous_same_mode_backup(
    output_dir: Path,
    *,
    db_name: str,
    mode: str,
    current_path: str,
) -> Path | None:
    """Return the newest prior dump for *db_name*/*mode*, excluding *current_path*.

    Returns None if no prior dumps exist. Used by the size-sanity check
    to detect implausibly small new backups.
    """
    current = Path(current_path).resolve()
    candidates = [
        p for p in output_dir.glob(f"{db_name}_{mode}_*.dump") if p.resolve() != current
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@dataclass
class BackupResult:
    """Result of a backup operation."""

    success: bool
    backup_path: str = ""
    error: str = ""


def run_backup(
    *,
    db_name: str,
    output_dir: str,
    database_url: str,
    compression: str = "zstd:9",
    mode: str = "full",
    excluded_tables: list[str] | None = None,
    jobs: int = 1,
) -> BackupResult:
    """Run pg_dump with custom-format compression.

    Args:
        db_name: Database name to back up.
        output_dir: Directory for the backup file.
        database_url: App connection URL — `pg_dump` only needs SELECT
            on the app's own tables, so the regular database_url is
            sufficient. No admin privileges required.
        compression: Compression spec (e.g. "zstd:9").
        mode: "full" or "slim" (slim excludes tables).
        excluded_tables: Tables to exclude in slim mode.
        jobs: Number of parallel pg_dump workers. When ``1`` (default),
            uses the single-stream custom format (``-Fc``) writing one
            ``.dump`` file. When ``>1``, switches to directory format
            (``-Fd -j N``) writing a ``.dump/`` directory containing
            ``toc.dat`` plus per-table ``*.dat`` blobs. Parallelism comes
            from concurrent table COPYs.
    """
    if not isinstance(jobs, int) or isinstance(jobs, bool) or jobs < 1:
        msg = f"jobs must be a positive integer, got {jobs!r}"
        raise ValueError(msg)
    validate_pg_identifier(db_name, "database name")
    _validate_compression(compression)
    validate_file_path(output_dir)

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M")
    algo = compression.split(":", maxsplit=1)[0]
    suffix = f"_{algo}" if algo != "none" else ""
    filename = f"{db_name}_{mode}_{timestamp}{suffix}.dump"
    backup_path = f"{output_dir}/{filename}"

    # pg_dump itself creates the directory for -Fd and refuses if it already exists.
    cmd: list[str] = ["pg_dump"]
    if jobs > 1:
        cmd.extend(["-Fd", "-j", str(jobs)])
    else:
        cmd.append("-Fc")
    cmd.extend([f"--compress={compression}", "-f", backup_path])

    if mode == "slim" and excluded_tables:
        for table in excluded_tables:
            validate_pg_identifier(table, "excluded table")
            cmd.extend(["-T", table])

    cmd.append(db_name)

    code, _, stderr = _pg_cmd(cmd, connection_url=database_url)

    if code != 0:
        return BackupResult(
            success=False,
            backup_path=backup_path,
            error=stderr.strip(),
        )

    toc_ok, toc_err = _verify_backup_toc(backup_path, connection_url=database_url)
    if not toc_ok:
        return BackupResult(
            success=False,
            backup_path=backup_path,
            error=f"backup failed TOC verification: {toc_err}",
        )

    prev = _previous_same_mode_backup(
        Path(output_dir), db_name=db_name, mode=mode, current_path=backup_path
    )
    if prev is not None:
        prev_size = _dump_size(prev)
        cur_size = _dump_size(Path(backup_path))
        if prev_size > 0 and cur_size < prev_size * _SIZE_SANITY_RATIO:
            return BackupResult(
                success=False,
                backup_path=backup_path,
                error=(
                    f"backup size sanity check failed: "
                    f"new={cur_size} bytes, prev={prev_size} bytes "
                    f"({cur_size * 100 // prev_size}% of previous)"
                ),
            )

    return BackupResult(success=True, backup_path=backup_path)


def free_space_gb(path: str | Path) -> float:
    """Free space on *path*'s volume, in GB.

    The one place free space is measured (#344). ``check_disk_space`` answers
    the producing side's yes/no question and ``doctor``'s corpus check needs the
    number itself to report it, so the measurement is here and both read it
    rather than each calling ``shutil.disk_usage``. Raises ``OSError`` if the
    volume cannot be read — callers must not treat that as "there is room".
    """
    return shutil.disk_usage(path).free / (1024**3)


def check_disk_space(path: str, *, required_gb: int) -> bool:
    """Return True if *path* has at least *required_gb* GB free."""
    return free_space_gb(path) >= required_gb


@dataclass(frozen=True)
class CleanupOutcome:
    """What one prune did, and why anything survived it.

    The three tuples partition the corpus — every candidate lands in
    exactly one. ``kept`` is within retention and would have survived
    with no floor at all; ``exempted_by_minimum`` is past the cutoff and
    survived *only* because the floor held it back. A dump inside the
    exemption slice that is still within retention is ``kept``, not
    exempted: the floor did not save it. Keeping those apart is the
    point — it is what makes :attr:`floor_was_load_bearing` knowable,
    and a caller wanting every survivor concatenates the two.

    An entry the containment guard refused — a symlink resolving outside
    ``backup_dir`` — appears in none of the three. It is not this
    directory's to hold, so counting it as kept would report a live
    corpus where there is none.

    :attr:`invalid` is an **overlay, not a fourth partition member** (#342).
    Every name in it also appears in exactly one of the three above, because
    validity decides whether a dump may *hold a floor slot* — it does not
    create a new fate. Adding it to the sum would double-count the corpus and
    break :attr:`floor_was_load_bearing`, which reads the partition.
    """

    removed: tuple[str, ...]
    kept: tuple[str, ...]
    exempted_by_minimum: tuple[str, ...]
    invalid: tuple[str, ...] = ()
    """Dumps ``pg_restore --list`` rejected while the floor was being allocated.

    Scoped deliberately: only candidates that actually contested a slot are
    verified, so this is not a corpus audit. A full sweep would shell out once
    per dump on every nightly prune. ``doctor`` is where the thorough check
    belongs, and the restore path verifies the dump it is about to restore.
    """

    @property
    def floor_was_load_bearing(self) -> bool:
        """Every survivor is older than the cutoff — the producer has stalled.

        Nothing was fresh enough to survive on its own, so the floor is
        the only reason the corpus is not empty. This is the state that
        preceded the #339 outage and it is only knowable at prune time.
        """
        return bool(self.exempted_by_minimum) and not self.kept


def _candidates(backup_dir: Path, match: str) -> list[tuple[Path, float]]:
    """Return *match*-ing dumps in *backup_dir* as (path, mtime), newest first."""
    entries = [(p, p.stat().st_mtime) for p in backup_dir.glob(match)]
    entries.sort(key=lambda entry: entry[1], reverse=True)
    return entries


def cleanup_old_backups(
    backup_dir: Path,
    *,
    retention_hours: int,
    match: str = "*.dump",
    keep_minimum: int = 0,
    dry_run: bool = False,
) -> CleanupOutcome:
    """Remove backup files and directory dumps older than *retention_hours*.

    Handles both ``-Fc`` file dumps (one ``.dump`` file) and ``-Fd``
    directory dumps (one ``.dump/`` directory tree). For each match,
    ``rmtree`` is guarded by a resolved-path containment check against
    ``backup_dir`` to ensure a glob result can't escape via a symlinked
    entry.

    *match* scopes the glob to one artifact class — full and slim dumps
    share a directory and expire on different clocks. Both the floor and
    the age rule apply within the matched set only.

    *keep_minimum* newest **valid** dumps — by mtime, not by filename — are
    exempt from the age rule entirely. The exemption is applied *before* the
    cutoff test rather than after, so the floor still holds in the case
    it exists for: a stalled producer leaves every dump in the corpus
    older than the cutoff, and the whole corpus would otherwise age out
    together.

    Validity entered the floor because of what it was protecting (#342). A
    dump still being written is the newest entry in the directory, so the
    floor's first act was to protect the corrupt file — and with a stalled
    producer, to hold that slot while every valid dump aged out around it.
    "The newest three are safe" reads like a validity guarantee; it was a
    count.

    The limit is exact, and narrower than "nothing new is deleted": an
    ``INVALID`` dump loses its *slot*, so a dump the floor used to hold can
    now be removed — that is the point. What is guaranteed is that **no valid
    dump is removed that the validity-blind floor would have kept**, and that
    anything newly removed is both unreadable and already past the cutoff. The
    floor shifts by one, so a valid dump that would have aged out survives in
    the corrupt file's place.

    ``UNVERIFIABLE`` spends slots normally: on a host with no ``pg_restore``
    every dump is unverifiable, and refusing them slots would turn a missing
    binary into a corpus-wide retention change.

    Verification is bounded to the candidates that actually contest a slot —
    nothing is checked when *keep_minimum* is 0, when nothing is past the
    cutoff, or once the floor is full. A full sweep would shell out once per
    dump on every nightly run.

    *dry_run* selects exactly as a real run does — including the
    containment guard — and deletes nothing. It is a parameter here rather
    than a candidate list rebuilt by the caller because a preview derived
    from a second implementation of "what expires" is not a preview of
    this one.

    Returns a :class:`CleanupOutcome` describing all three groups, plus the
    ``invalid`` overlay.
    """
    cutoff = time.time() - retention_hours * 3600
    resolved_root = backup_dir.resolve()
    candidates = _candidates(backup_dir, match)
    removed: list[str] = []
    kept: list[str] = []
    exempted: list[str] = []
    invalid: list[str] = []

    # Slots consumed so far, not the enumerate index. They coincide exactly
    # when every candidate is valid — which is what keeps a corpus this
    # cannot verify retaining as it does today.
    slots = 0

    for f, mtime in candidates:
        if mtime >= cutoff:
            kept.append(str(f))
            slots += 1
            continue
        if slots < keep_minimum:
            check = verify_archive(f)
            if check.is_bad:
                # No slot spent, so the next valid dump takes this one. Falls
                # through to the age rule, which removes it only because it
                # is already past the cutoff.
                invalid.append(str(f))
                log.warning(
                    "Backup %s is not a readable archive and will not hold a "
                    "keep_minimum slot: %s",
                    f,
                    check.detail,
                )
            else:
                exempted.append(str(f))
                slots += 1
                continue
        resolved = f.resolve()
        if not resolved.is_relative_to(resolved_root):
            continue
        if not dry_run:
            if f.is_dir():
                shutil.rmtree(f)
            else:
                f.unlink()
        removed.append(str(f))

    return CleanupOutcome(
        removed=tuple(removed),
        kept=tuple(kept),
        exempted_by_minimum=tuple(exempted),
        invalid=tuple(invalid),
    )


def should_run_now(
    schedule: str,
    *,
    hour: int | None = None,
    minute: int | None = None,
) -> bool:
    """Check if a backup should run at the given time.

    Supports two formats:
    - "HH:MM" — exact time match
    - "*/N *" — every N hours at minute 0
    """
    now = datetime.now(tz=UTC)
    h = hour if hour is not None else now.hour
    m = minute if minute is not None else now.minute

    if ":" in schedule and "/" not in schedule:
        # "HH:MM" format
        parts = schedule.split(":")
        return int(parts[0]) == h and int(parts[1]) == m

    if schedule.startswith("*/"):
        # "*/N *" cron-style format
        interval = int(schedule.split("/")[1].split(maxsplit=1)[0])
        return h % interval == 0 and m == 0

    return False

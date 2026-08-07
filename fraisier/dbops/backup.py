"""Backup runner: pg_dump with compression, retention, and scheduling.

Supports full and slim backup modes, disk space checks, retention
cleanup, and per-destination schedule matching.
"""

import re
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fraisier.dbops._validation import validate_file_path, validate_pg_identifier
from fraisier.dbops.operations import _pg_cmd

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
    connection_url: str,
) -> tuple[bool, str]:
    """Verify a backup file's TOC by running ``pg_restore --list``.

    No database connection is required — ``--list`` reads the archive's
    header only. Catches truncated/corrupt dumps that would otherwise be
    discovered hours later during a restore attempt.

    Returns (True, "") on success, (False, stderr) on failure.
    """
    cmd = ["pg_restore", "--list", backup_path]
    code, _, stderr = _pg_cmd(cmd, connection_url=connection_url)
    if code != 0:
        return False, stderr.strip()
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


def check_disk_space(path: str, *, required_gb: int) -> bool:
    """Return True if *path* has at least *required_gb* GB free."""
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024**3)
    return free_gb >= required_gb


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
    """

    removed: tuple[str, ...]
    kept: tuple[str, ...]
    exempted_by_minimum: tuple[str, ...]

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

    *keep_minimum* newest dumps — by mtime, not by filename — are exempt
    from the age rule entirely. The exemption is applied *before* the
    cutoff test rather than after, so the floor still holds in the case
    it exists for: a stalled producer leaves every dump in the corpus
    older than the cutoff, and the whole corpus would otherwise age out
    together.

    Returns a :class:`CleanupOutcome` describing all three groups.
    """
    cutoff = time.time() - retention_hours * 3600
    resolved_root = backup_dir.resolve()
    candidates = _candidates(backup_dir, match)
    removed: list[str] = []
    kept: list[str] = []
    exempted: list[str] = []

    for index, (f, mtime) in enumerate(candidates):
        if mtime >= cutoff:
            kept.append(str(f))
            continue
        if index < keep_minimum:
            exempted.append(str(f))
            continue
        resolved = f.resolve()
        if not resolved.is_relative_to(resolved_root):
            continue
        if f.is_dir():
            shutil.rmtree(f)
        else:
            f.unlink()
        removed.append(str(f))

    return CleanupOutcome(
        removed=tuple(removed),
        kept=tuple(kept),
        exempted_by_minimum=tuple(exempted),
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

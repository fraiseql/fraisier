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
    """
    validate_pg_identifier(db_name, "database name")
    _validate_compression(compression)
    validate_file_path(output_dir)

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M")
    algo = compression.split(":", maxsplit=1)[0]
    suffix = f"_{algo}" if algo != "none" else ""
    filename = f"{db_name}_{mode}_{timestamp}{suffix}.dump"
    backup_path = f"{output_dir}/{filename}"

    cmd: list[str] = [
        "pg_dump",
        "-Fc",
        f"--compress={compression}",
        "-f",
        backup_path,
    ]

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
        prev_size = prev.stat().st_size
        cur_size = Path(backup_path).stat().st_size
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


def cleanup_old_backups(
    backup_dir: Path,
    *,
    retention_hours: int,
) -> list[str]:
    """Remove backup files older than *retention_hours*.

    Returns list of removed file paths.
    """
    cutoff = time.time() - retention_hours * 3600
    removed: list[str] = []

    for f in backup_dir.glob("*.dump"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed.append(str(f))

    return removed


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

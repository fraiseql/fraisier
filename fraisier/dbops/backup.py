"""Backup runner: pg_dump with compression, retention, and scheduling.

Supports full and slim backup modes, disk space checks, retention
cleanup, and per-destination schedule matching.
"""

import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fraisier.dbops._validation import validate_pg_identifier


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
    compression: str = "zstd:9",
    mode: str = "full",
    excluded_tables: list[str] | None = None,
    sudo_user: str = "postgres",
) -> BackupResult:
    """Run pg_dump with custom-format compression.

    Args:
        db_name: Database name to back up.
        output_dir: Directory for the backup file.
        compression: Compression spec (e.g. "zstd:9").
        mode: "full" or "slim" (slim excludes tables).
        excluded_tables: Tables to exclude in slim mode.
        sudo_user: OS user to run pg_dump as.
    """
    validate_pg_identifier(db_name, "database name")
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M")
    filename = f"{db_name}_{mode}_{timestamp}.dump"
    backup_path = f"{output_dir}/{filename}"

    cmd = [
        "sudo",
        "-u",
        sudo_user,
        "pg_dump",
        "-Fc",
        f"--compress={compression}",
        "-f",
        backup_path,
    ]

    if mode == "slim" and excluded_tables:
        for table in excluded_tables:
            cmd.extend(["-T", table])

    cmd.append(db_name)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        return BackupResult(
            success=False,
            backup_path=backup_path,
            error=result.stderr.strip(),
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

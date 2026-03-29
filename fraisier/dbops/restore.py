"""Staging restore: pg_restore with ownership fix and table validation.

Restores a production backup into a staging database, fixes ownership,
and validates the restore by checking the table count against a minimum
threshold.
"""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from fraisier.dbops._validation import validate_file_path, validate_pg_identifier
from fraisier.dbops.operations import _pg_cmd


@dataclass
class RestoreResult:
    """Result of a restore operation."""

    success: bool
    error: str = ""


def restore_backup(
    *,
    backup_path: str,
    db_name: str,
    db_owner: str | None = None,
    sudo_user: str = "postgres",
) -> RestoreResult:
    """Restore a pg_dump backup into *db_name*.

    Optionally reassigns ownership to *db_owner* after restore.
    """
    validate_pg_identifier(db_name, "database name")
    validate_file_path(backup_path)
    if db_owner:
        validate_pg_identifier(db_owner, "database owner")

    # Run pg_restore
    code, _, stderr = _pg_cmd(
        ["pg_restore", "-d", db_name, "--no-owner", "--no-acl", backup_path],
        sudo_user=sudo_user,
    )
    if code != 0:
        return RestoreResult(success=False, error=stderr.strip())

    # Fix ownership if requested — use psql variable binding to prevent injection
    if db_owner:
        rc, _, stderr = _pg_cmd(
            [
                "psql",
                "-d",
                db_name,
                "-v",
                f"owner={db_owner}",
                "-c",
                'REASSIGN OWNED BY CURRENT_USER TO :"owner"',
            ],
            sudo_user=sudo_user,
        )
        if rc != 0:
            return RestoreResult(
                success=False,
                error=f"Ownership reassignment to {db_owner} failed: {stderr.strip()}",
            )

    return RestoreResult(success=True)


def validate_table_count(
    db_name: str,
    *,
    min_threshold: int = 50,
    sudo_user: str = "postgres",
) -> tuple[bool, int]:
    """Check that *db_name* has at least *min_threshold* tables.

    Returns (ok, count).
    """
    sql = "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
    result = subprocess.run(
        ["sudo", "-u", sudo_user, "psql", "-d", db_name, "-t", "-A", "-c", sql],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False, 0

    try:
        count = int(result.stdout.strip())
    except ValueError:
        return False, 0

    return count >= min_threshold, count


def find_latest_backup(
    backup_dir: Path,
    *,
    pattern: str = "*.dump",
) -> Path | None:
    """Find the most recent backup file matching *pattern* in *backup_dir*."""
    dumps = list(backup_dir.glob(pattern))
    if not dumps:
        return None
    return max(dumps, key=lambda p: p.stat().st_mtime)


def validate_backup_age(
    backup_path: Path,
    *,
    max_age_hours: float,
) -> bool:
    """Return True if *backup_path* is newer than *max_age_hours*."""
    age_seconds = time.time() - backup_path.stat().st_mtime
    return age_seconds < max_age_hours * 3600

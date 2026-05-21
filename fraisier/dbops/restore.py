"""Staging restore: pg_restore with ownership fix and table validation.

Restores a production backup into a staging database, fixes ownership,
and validates the restore by checking the table count against a minimum
threshold.
"""

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
    duration_seconds: float = 0.0


def restore_backup(
    *,
    backup_path: str,
    db_name: str,
    connection_url: str,
    db_owner: str | None = None,
    jobs: int = 1,
) -> RestoreResult:
    """Restore a pg_dump backup into *db_name*.

    Optionally reassigns ownership to *db_owner* after restore.
    """
    validate_pg_identifier(db_name, "database name")
    validate_file_path(backup_path)
    if db_owner:
        validate_pg_identifier(db_owner, "database owner")

    # Run pg_restore
    t0 = time.monotonic()
    cmd = ["pg_restore", "-d", db_name, "--no-owner", "--no-acl"]
    if jobs > 1:
        cmd.extend(["-j", str(jobs)])
    cmd.append(backup_path)
    code, _, stderr = _pg_cmd(cmd, connection_url=connection_url)
    duration = time.monotonic() - t0
    if code != 0:
        return RestoreResult(
            success=False, error=stderr.strip(), duration_seconds=duration
        )

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
            connection_url=connection_url,
        )
        if rc != 0:
            return RestoreResult(
                success=False,
                error=f"Ownership reassignment to {db_owner} failed: {stderr.strip()}",
                duration_seconds=time.monotonic() - t0,
            )

    return RestoreResult(success=True, duration_seconds=time.monotonic() - t0)


def validate_table_count(
    db_name: str,
    *,
    connection_url: str,
    min_threshold: int = 50,
) -> tuple[bool, int]:
    """Check that *db_name* has at least *min_threshold* tables.

    Returns (ok, count).
    """
    sql = "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
    code, stdout, _ = _pg_cmd(
        ["psql", "-d", db_name, "-t", "-A", "-c", sql],
        connection_url=connection_url,
    )
    if code != 0:
        return False, 0

    try:
        count = int(stdout.strip())
    except ValueError:
        return False, 0

    return count >= min_threshold, count


def find_latest_backup(
    backup_dir: Path,
    *,
    pattern: str = "*.dump",
    preferred_compression: str | None = None,
) -> Path | None:
    """Find the most recent backup file or directory matching *pattern* in *backup_dir*.

    Discovers both ``pg_dump -Fc`` file dumps (``*.dump``) and parallel
    ``pg_dump -Fd`` directory dumps (``*.dump/`` containing ``toc.dat``
    plus per-table ``*.dat`` blobs). The producer side names both forms
    with the same ``<db>_<mode>_<ts>_<algo>.dump`` convention so a single
    glob covers both; ordering is by mtime, with the directory's own
    mtime reflecting the moment ``pg_dump`` finished writing the last
    ``*.dat`` block.

    When *preferred_compression* is set (e.g. ``"lz4"``), tries to find
    the newest dump whose filename contains ``_{algo}.dump`` first
    (matching either form). Falls back to the regular newest-dump
    selection if none match.
    """
    if preferred_compression:
        pref_pattern = f"*_{preferred_compression}.dump"
        pref_dumps = list(backup_dir.glob(pref_pattern))
        if pref_dumps:
            return max(pref_dumps, key=lambda p: p.stat().st_mtime)

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

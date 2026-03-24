"""Confiture integration — thin policy layer over confiture's Python API.

Provides two interfaces:

1. **Python API** (new in v0.3): ``preflight()``, ``migrate_up()``,
   ``migrate_down()``, ``has_pending()`` — used by strategies and deployers.

2. **CLI wrappers** (kept for docker-compose and CLI commands):
   ``confiture_migrate()``, ``confiture_rebuild()``, ``confiture_status()``
   — run ``confiture`` as a subprocess.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from confiture.core.locking import LockAcquisitionError
from confiture.core.migrator import Migrator

if TYPE_CHECKING:
    from confiture import MigrateDownResult, MigrateUpResult

log = logging.getLogger(__name__)

MAX_LOCK_RETRIES = 3


# ---------------------------------------------------------------------------
# Fraisier result types (decoupled from confiture internals)
# ---------------------------------------------------------------------------


@dataclass
class MigrationResult:
    """Fraisier's view of a migration outcome."""

    success: bool
    steps_applied: int = 0
    errors: list[str] = field(default_factory=list)
    execution_time_ms: int = 0


class IrreversibleMigrationError(Exception):
    """Raised when pending migrations lack down files."""


class MigrationIntegrityError(Exception):
    """Raised when duplicate versions are detected."""


class MigrationError(Exception):
    """Raised when confiture migrate up fails."""


class RollbackError(Exception):
    """Raised when confiture migrate down fails."""


# ---------------------------------------------------------------------------
# Python API — used by strategies
# ---------------------------------------------------------------------------


def preflight(
    config_path: Path | str,
    *,
    migrations_dir: Path | str = "db/migrations",
    allow_irreversible: bool = False,
) -> None:
    """Run pre-deploy checks.  Raises on policy violations.

    Checks:
    - No duplicate migration versions
    - All pending migrations have down files (unless *allow_irreversible*)
    """
    mdir = Path(migrations_dir)

    with Migrator.from_config(config_path, migrations_dir=mdir) as m:
        status = m.status()

    if not status.has_pending:
        return

    # Check for irreversible pending migrations by scanning for down files
    if not allow_irreversible:
        pending_versions = set(status.pending)
        for up_file in mdir.glob("*.up.sql"):
            version = up_file.name.split("_", 1)[0]
            if version in pending_versions:
                down_file = up_file.with_suffix("").with_suffix(".down.sql")
                if not down_file.exists():
                    raise IrreversibleMigrationError(
                        f"Irreversible migration: {version} (no down file). "
                        "Use --no-rollback to deploy without automatic rollback."
                    )


def dry_run_execute(
    config_path: Path | str,
    *,
    migrations_dir: Path | str = "db/migrations",
) -> MigrationResult:
    """Run migrations inside a SAVEPOINT, then rollback.

    Uses confiture's native ``dry_run_execute`` parameter (v0.8.11+)
    to catch real SQL errors without making permanent changes.
    """
    start = time.monotonic()

    try:
        mdir = Path(migrations_dir)
        with Migrator.from_config(config_path, migrations_dir=mdir) as m:
            result = m.up(dry_run_execute=True)

        elapsed_ms = int((time.monotonic() - start) * 1000)

        if result.has_errors:
            return MigrationResult(
                success=False,
                steps_applied=0,
                errors=[result.error_summary or "dry-run-execute failed"],
                execution_time_ms=elapsed_ms,
            )

        log.info("Dry-run-execute passed")
        return MigrationResult(
            success=True,
            steps_applied=0,
            execution_time_ms=elapsed_ms,
        )
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return MigrationResult(
            success=False,
            steps_applied=0,
            errors=[str(e)],
            execution_time_ms=elapsed_ms,
        )


def migrate_up(
    config_path: Path | str,
    *,
    migrations_dir: Path | str = "db/migrations",
    lock_timeout: int = 30_000,
    pre_migrate_verify: bool = False,
    require_reversible: bool = False,
) -> MigrationResult:
    """Apply pending migrations with lock retry.

    Args:
        config_path: Path to confiture.yaml.
        migrations_dir: Path to migrations directory.
        lock_timeout: Lock timeout in milliseconds.
        pre_migrate_verify: When True, run a dry-run-execute first to
            catch SQL errors before applying for real.
        require_reversible: When True, abort if any pending migration
            lacks a .down.sql file (confiture v0.8.11+).
    """
    if pre_migrate_verify:
        verify_result = dry_run_execute(config_path, migrations_dir=migrations_dir)
        if not verify_result.success:
            raise MigrationError(
                "Pre-migration verification failed: " + "; ".join(verify_result.errors)
            )
        log.info("Pre-migration verification passed")

    start = time.monotonic()

    with Migrator.from_config(config_path, migrations_dir=Path(migrations_dir)) as m:
        for attempt in range(MAX_LOCK_RETRIES):
            try:
                result: MigrateUpResult = m.up(
                    lock_timeout=lock_timeout,
                    require_reversible=require_reversible,
                )
                break
            except LockAcquisitionError:
                if attempt == MAX_LOCK_RETRIES - 1:
                    raise
                wait = 2**attempt
                log.warning(
                    "Lock contention, retrying in %ds (attempt %d/%d)",
                    wait,
                    attempt + 1,
                    MAX_LOCK_RETRIES,
                )
                time.sleep(wait)

    elapsed_ms = int((time.monotonic() - start) * 1000)

    if result.has_errors:
        raise MigrationError(f"Migration failed: {result.error_summary}")

    return MigrationResult(
        success=True,
        steps_applied=len(result.migrations_applied),
        errors=[],
        execution_time_ms=elapsed_ms,
    )


def migrate_down(
    config_path: Path | str,
    *,
    migrations_dir: Path | str = "db/migrations",
    steps: int,
) -> MigrationResult:
    """Reverse exactly *steps* migrations.  Best-effort — logs errors."""
    start = time.monotonic()

    with Migrator.from_config(config_path, migrations_dir=Path(migrations_dir)) as m:
        result: MigrateDownResult = m.down(steps=steps)

    elapsed_ms = int((time.monotonic() - start) * 1000)

    if not result.success:
        log.critical("Rollback failed: %s — manual intervention required", result.error)
        return MigrationResult(
            success=False,
            steps_applied=0,
            errors=[str(result.error)] if result.error else [],
            execution_time_ms=elapsed_ms,
        )

    return MigrationResult(
        success=True,
        steps_applied=len(result.migrations_rolled_back),
        errors=[],
        execution_time_ms=elapsed_ms,
    )


def has_pending(
    config_path: Path | str,
    *,
    migrations_dir: Path | str = "db/migrations",
) -> bool:
    """Check if there are pending migrations."""
    with Migrator.from_config(config_path, migrations_dir=Path(migrations_dir)) as m:
        return m.status().has_pending


# ---------------------------------------------------------------------------
# CLI subprocess wrappers (kept for docker-compose provider and CLI)
# ---------------------------------------------------------------------------


@dataclass
class ConfitureResult:
    """Result of a confiture subprocess operation."""

    success: bool
    exit_code: int = 0
    migration_count: int = 0
    stdout: str = ""
    error: str = ""
    error_type: str = ""


@dataclass
class StatusResult:
    """Result of ``confiture migrate status``."""

    exit_code: int
    has_pending: bool = False
    pending_count: int = 0
    applied_count: int = 0
    pending: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    tracking_table_missing: bool = False
    fatal: bool = False
    error: str = ""
    raw: str = ""


_MIGRATION_COUNT_RE = re.compile(r"(?:Applied|Rolled back)\s+(\d+)\s+migration")

_SCHEMA_ERROR_PATTERNS = (
    "already exists",
    "does not exist",
    "duplicate key",
    "syntax error",
    "violates",
)

_CONNECTION_ERROR_PATTERNS = (
    "connection refused",
    "could not connect",
    "timeout expired",
)

_LOCK_ERROR_PATTERNS = (
    "lock timeout",
    "could not obtain lock",
    "cannot acquire",
)


def parse_migration_count(output: str) -> int:
    """Extract migration count from confiture stdout."""
    m = _MIGRATION_COUNT_RE.search(output)
    return int(m.group(1)) if m else 0


def classify_error(stderr: str) -> str:
    """Classify a confiture error message."""
    lower = stderr.lower()
    if any(p in lower for p in _LOCK_ERROR_PATTERNS):
        return "lock_error"
    if any(p in lower for p in _SCHEMA_ERROR_PATTERNS):
        return "schema_error"
    if any(p in lower for p in _CONNECTION_ERROR_PATTERNS):
        return "connection_error"
    return "unknown"


def _classify_exit_code(exit_code: int) -> str:
    """Classify error type from confiture exit code."""
    return {
        2: "validation_error",
        3: "migration_error",
        6: "lock_error",
    }.get(exit_code, "unknown")


def confiture_migrate(
    *,
    config_path: str = "confiture.yaml",
    cwd: str = ".",
    direction: str = "up",
    auto_detect_baseline: bool = False,
) -> ConfitureResult:
    """Run ``confiture migrate up`` or ``confiture migrate down``."""
    cmd = ["confiture", "migrate", direction, "-c", config_path]
    if auto_detect_baseline and direction == "up":
        cmd.append("--auto-detect-baseline")

    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        error_type = classify_error(result.stderr)
        if error_type == "unknown":
            error_type = _classify_exit_code(result.returncode)
        return ConfitureResult(
            success=False,
            exit_code=result.returncode,
            stdout=result.stdout,
            error=result.stderr.strip(),
            error_type=error_type,
        )

    return ConfitureResult(
        success=True,
        exit_code=0,
        migration_count=parse_migration_count(result.stdout),
        stdout=result.stdout,
    )


def confiture_rebuild(
    *,
    config_path: str = "confiture.yaml",
    cwd: str = ".",
    drop_schemas: bool = True,
) -> ConfitureResult:
    """Run ``confiture migrate rebuild``."""
    cmd = ["confiture", "migrate", "rebuild", "-c", config_path, "-y"]
    if drop_schemas:
        cmd.append("--drop-schemas")

    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        return ConfitureResult(
            success=False,
            exit_code=result.returncode,
            stdout=result.stdout,
            error=result.stderr.strip(),
            error_type=classify_error(result.stderr),
        )

    return ConfitureResult(
        success=True,
        exit_code=0,
        migration_count=parse_migration_count(result.stdout),
        stdout=result.stdout,
    )


def confiture_status(
    *,
    config_path: str = "confiture.yaml",
    cwd: str = ".",
) -> StatusResult:
    """Run ``confiture migrate status --format json``."""
    cmd = ["confiture", "migrate", "status", "-c", config_path, "--format", "json"]

    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)

    status = StatusResult(exit_code=result.returncode, raw=result.stdout)

    if result.returncode == 3:
        status.fatal = True
        status.error = result.stderr.strip()
        return status

    if result.returncode == 2:
        status.tracking_table_missing = True
        status.error = result.stderr.strip()
        return status

    try:
        data = json.loads(result.stdout)
        summary = data.get("summary", {})
        status.applied_count = summary.get("applied", 0)
        status.pending_count = summary.get("pending", 0)
        migrations = data.get("migrations", [])
        status.applied = [
            m["version"] for m in migrations if m.get("status") == "applied"
        ]
        status.pending = [
            m["version"] for m in migrations if m.get("status") == "pending"
        ]
        status.has_pending = result.returncode == 1
    except (json.JSONDecodeError, KeyError):
        status.error = f"Failed to parse status JSON: {result.stdout[:200]}"

    return status


def confiture_build(
    *,
    config_path: str = "confiture.yaml",
    cwd: str = ".",
    rebuild: bool = False,
) -> ConfitureResult:
    """Backward-compatible wrapper.

    .. deprecated::
        Use ``confiture_migrate()`` or ``confiture_rebuild()``.
    """
    if rebuild:
        return confiture_rebuild(config_path=config_path, cwd=cwd)
    return confiture_migrate(config_path=config_path, cwd=cwd, direction="up")

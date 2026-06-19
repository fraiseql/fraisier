"""Migration preflight: schema extraction, temp DB lifecycle, confiture integration.

Provides the building blocks for running migration preflight checks before a
full pg_restore:

1. ``extract_schema_only`` — extract schema-only SQL from a pg_dump backup
2. ``PreflightDatabase`` — create/destroy a temporary preflight database
3. ``MigrationCheck`` / ``MigrationPreflightResult`` — structured result types
4. ``run_migration_preflight`` — orchestrate the full preflight flow
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase 1: Schema extraction + temp DB lifecycle
# ---------------------------------------------------------------------------


def extract_schema_only(
    backup_path: Path,
    output_dir: Path | None = None,
) -> Path:
    """Extract schema-only SQL from a pg_dump backup file.

    Runs ``pg_restore --schema-only`` on custom-format dumps (.dump, etc.)
    to produce a plain-SQL file containing only DDL statements (no data).

    Args:
        backup_path: Path to the pg_dump backup file (custom format).
        output_dir: Directory to write the schema file. A temporary directory
            is created automatically when ``None``.

    Returns:
        Path to the extracted schema SQL file.

    Raises:
        DatabaseError: If ``pg_restore`` exits with a non-zero return code.
    """
    from fraisier.errors import DatabaseError

    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="fraisier_preflight_"))

    schema_file = output_dir / f"{backup_path.stem}_schema.sql"

    result = subprocess.run(
        [
            "pg_restore",
            "--schema-only",
            "--no-owner",
            "--no-acl",
            "--file",
            str(schema_file),
            str(backup_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise DatabaseError(f"Schema extraction failed: {result.stderr}")

    return schema_file


def _run_admin_sql(admin_url: str, sql: str) -> subprocess.CompletedProcess[str]:
    """Run a SQL statement against the admin database URL.

    Extracts host/port/user/password from *admin_url* and passes them as CLI
    flags / ``PGPASSWORD`` environment variable so no password appears in the
    process table.

    Args:
        admin_url: PostgreSQL connection URL for the admin/maintenance database.
        sql: SQL statement to execute.

    Returns:
        CompletedProcess with returncode, stdout, stderr.
    """
    from fraisier.dbops.operations import _parse_connection_flags

    conn_flags, extra_env = _parse_connection_flags(admin_url)
    parsed = urlparse(admin_url)
    db = parsed.path.lstrip("/")
    if db:
        conn_flags = [*conn_flags, "-d", db]

    run_env = {**os.environ, **extra_env} if extra_env else None
    return subprocess.run(
        ["psql", *conn_flags, "-c", sql],
        check=False,
        capture_output=True,
        text=True,
        env=run_env,
    )


def _terminate_preflight_connections(admin_url: str, db_name: str) -> None:
    """Terminate active connections to *db_name* so the database can be dropped."""
    sql = (
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
    )
    _run_admin_sql(admin_url, sql)


@dataclass
class PreflightDatabase:
    """Context manager for a temporary preflight database.

    Creates a randomised ``fraisier_preflight_<hex>`` database on ``__enter__``,
    and drops it (with connection termination) on ``__exit__``.  The database is
    always dropped, even when the ``with`` block raises an exception.

    Attributes:
        admin_url: PostgreSQL admin URL (must point to an existing maintenance DB,
            e.g. ``postgresql://admin@localhost/postgres``).
        db_name: Generated name of the temporary database (available after entry).
        url: Full connection URL for the temporary database (available after entry).

    Example::

        with PreflightDatabase(admin_url="postgresql://admin@localhost/postgres") as pf:
            pf.restore_schema(schema_path)
            # pf.url is now a usable connection string
    """

    admin_url: str
    db_name: str = field(init=False)
    url: str = field(init=False)

    def __post_init__(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.db_name = f"fraisier_preflight_{suffix}"
        self.url = ""

    def __enter__(self) -> PreflightDatabase:
        self._create()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        try:
            self._drop()
        except Exception:
            log.warning(
                "Failed to drop preflight database %s — manual cleanup may be needed",
                self.db_name,
                exc_info=True,
            )

    def _create(self) -> None:
        from fraisier.dbops._url import replace_db_name
        from fraisier.errors import DatabaseError

        result = _run_admin_sql(self.admin_url, f"CREATE DATABASE {self.db_name}")
        if result.returncode != 0:
            raise DatabaseError(
                f"Failed to create preflight database {self.db_name}: {result.stderr}"
            )
        self.url = replace_db_name(self.admin_url, self.db_name)

    def _drop(self) -> None:
        _terminate_preflight_connections(self.admin_url, self.db_name)
        _run_admin_sql(self.admin_url, f"DROP DATABASE IF EXISTS {self.db_name}")

    def restore_schema(self, schema_path: Path) -> None:
        """Restore a schema-only SQL file into this preflight database.

        Runs ``psql -f schema_path`` against ``self.url``.

        Args:
            schema_path: Path to the schema SQL file produced by
                ``extract_schema_only()``.

        Raises:
            DatabaseError: If ``psql`` exits with a non-zero return code.
        """
        from fraisier.dbops.operations import _parse_connection_flags
        from fraisier.errors import DatabaseError

        conn_flags, extra_env = _parse_connection_flags(self.url)
        parsed = urlparse(self.url)
        db = parsed.path.lstrip("/")
        if db:
            conn_flags = [*conn_flags, "-d", db]
        run_env = {**os.environ, **extra_env} if extra_env else None

        result = subprocess.run(
            ["psql", *conn_flags, "-f", str(schema_path)],
            check=False,
            capture_output=True,
            text=True,
            env=run_env,
        )
        if result.returncode != 0:
            raise DatabaseError(
                f"Schema restore into {self.db_name} failed: {result.stderr}"
            )


# ---------------------------------------------------------------------------
# Phase 2: Result types + confiture integration
# ---------------------------------------------------------------------------


@dataclass
class MigrationCheck:
    """Result of a single migration in a preflight run.

    Attributes:
        version: Migration version string (e.g. ``"20260429120000"``).
        name: Human-readable migration name (e.g. ``"add_email_column"``).
        passed: ``True`` if the migration executed without error.
        error: Error message when ``passed=False``; ``None`` otherwise.
        time_ms: Execution time in milliseconds.
        skipped: ``True`` when the migration was skipped (non-transactional).
        skipped_reason: Reason for skipping, or ``None``.
    """

    version: str
    name: str
    passed: bool
    error: str | None = None
    time_ms: int = 0
    skipped: bool = False
    skipped_reason: str | None = None


@dataclass
class MigrationPreflightResult:
    """Aggregated result of a ``run_migration_preflight()`` call.

    Attributes:
        migrations: Per-migration results.
        schema_extraction_ms: Time spent extracting the schema from the backup.
        total_ms: Total wall-clock time for the entire preflight run.
    """

    migrations: list[MigrationCheck]
    schema_extraction_ms: int = 0
    total_ms: int = 0

    @property
    def all_passed(self) -> bool:
        """``True`` if every non-skipped migration passed."""
        return all(m.passed for m in self.migrations if not m.skipped)

    @property
    def failure_count(self) -> int:
        """Number of migrations that failed (not skipped)."""
        return sum(1 for m in self.migrations if not m.skipped and not m.passed)

    @property
    def failures(self) -> list[MigrationCheck]:
        """Migrations that failed (not skipped)."""
        return [m for m in self.migrations if not m.skipped and not m.passed]

    @property
    def skipped_migrations(self) -> list[MigrationCheck]:
        """Migrations skipped during preflight (non-transactional)."""
        return [m for m in self.migrations if m.skipped]

    @property
    def suspected_false_positive_failures(self) -> list[MigrationCheck]:
        """Failures that likely stem from an earlier skipped migration.

        A non-transactional migration (e.g. ``CREATE INDEX CONCURRENTLY``,
        ``ALTER TYPE … ADD VALUE``) is skipped during preflight because it
        cannot run inside the SAVEPOINT the check uses.  A later pending
        migration that depends on the object such a migration would create then
        fails with ``"… does not exist"`` — a false alarm, since the skipped
        migration runs for real during a normal ``migrate up``.

        Returns the failures matching that signature: at least one earlier
        migration was skipped, and the failure is a missing-object error on a
        later version.  Empty when no migration was skipped (a missing-object
        error with no skip is a genuine failure, not a false positive).
        """
        skipped = self.skipped_migrations
        if not skipped:
            return []
        earliest_skipped = min(m.version for m in skipped)
        return [
            m
            for m in self.failures
            if m.error
            and "does not exist" in m.error.lower()
            and m.version > earliest_skipped
        ]

    @property
    def false_positive_note(self) -> str | None:
        """Diagnostic note when failures look like a non-transactional false alarm.

        ``None`` when no failure matches the skipped-dependency signature.
        """
        suspected = self.suspected_false_positive_failures
        if not suspected:
            return None
        return (
            f"{len(suspected)} of the failing migration(s) reference objects "
            "created by earlier pending migration(s) that were skipped as "
            "non-transactional (they cannot run inside preflight's SAVEPOINT). "
            "If these migrations apply cleanly in order during a normal "
            "`migrate up`, this is likely a preflight false positive — re-run "
            "the restore with `--skip-preflight` to bypass the check."
        )


def _run_confiture_preflight(
    confiture_config: Path,
    migrations_dir: Path,
    against_url: str,
    *,
    timeout_seconds: int = 120,
) -> MigrationPreflightResult:
    """Run ``confiture migrate preflight --against`` via subprocess.

    Invokes the confiture CLI with ``--format json`` so the output can be
    parsed into a structured ``MigrationPreflightResult``.  Exit code 0 means
    all passed; exit code 1 means failures were detected — both are valid
    preflight outcomes.  Any other exit code indicates a fatal error.

    Args:
        confiture_config: Path to the confiture config file.  Used to connect
            to the source database to determine which migrations are pending.
        migrations_dir: Directory containing migration files.
        against_url: PostgreSQL URL of the temporary preflight database.
        timeout_seconds: Maximum wall-clock time for the preflight subprocess.

    Returns:
        ``MigrationPreflightResult`` with one entry per pending migration.

    Raises:
        DatabaseError: If confiture exits with an unexpected error code.
    """
    from fraisier.errors import DatabaseError

    confiture_exe = str(Path(sys.executable).parent / "confiture")
    cmd = [
        confiture_exe,
        "migrate",
        "preflight",
        "--against",
        against_url,
        "--config",
        str(confiture_config),
        "--migrations-dir",
        str(migrations_dir),
        "--format",
        "json",
    ]
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )

    # 0 = all passed, 1 = failures detected — both are valid preflight outcomes.
    if result.returncode not in (0, 1):
        raise DatabaseError(
            f"confiture preflight failed (exit {result.returncode}): {result.stderr}"
        )

    data = json.loads(result.stdout)
    # Output format: {"static": {...}, "against": {...}} when --against is used.
    against_data: dict = data.get("against", data)

    migrations = [
        MigrationCheck(
            version=m["version"],
            name=m["name"],
            passed=m["success"],
            error=m.get("error"),
            time_ms=m.get("execution_time_ms", 0),
            skipped=m.get("skipped", False),
            skipped_reason=m.get("skipped_reason"),
        )
        for m in against_data.get("migrations", [])
    ]

    return MigrationPreflightResult(migrations=migrations)


def run_migration_preflight(
    backup_path: Path,
    admin_url: str,
    confiture_config: Path,
    migrations_dir: Path,
    *,
    timeout_seconds: int = 120,
) -> MigrationPreflightResult:
    """Run all pending migrations against a schema-only copy of the backup.

    Orchestrates the full preflight flow:

    1. Extract schema-only SQL from *backup_path* (~2 s).
    2. Create a temporary ``fraisier_preflight_*`` database.
    3. Restore the schema into the temporary database.
    4. Run ``confiture migrate preflight --against <temp_db>`` to test every
       pending migration inside a SAVEPOINT (always rolled back).
    5. Drop the temporary database.
    6. Return a structured ``MigrationPreflightResult``.

    The original database is **never touched**.  The temporary database is
    always dropped, even when a step raises an exception.

    Args:
        backup_path: Path to the pg_dump backup file (custom format).
        admin_url: PostgreSQL admin URL (maintenance DB, e.g.
            ``postgresql://admin@localhost/postgres``).
        confiture_config: Path to the confiture config file (used to detect
            pending migrations from the source database).
        migrations_dir: Directory containing migration files.
        timeout_seconds: Maximum wall-clock time for the preflight subprocess
            (default 120 s).

    Returns:
        ``MigrationPreflightResult`` with per-migration pass/fail results,
        schema extraction time, and total elapsed time.

    Raises:
        DatabaseError: If schema extraction, temp DB creation, schema restore,
            or the confiture call fails with a fatal error.
    """
    import time

    start = time.monotonic()

    schema_path: Path | None = None
    try:
        # Step 1: Extract schema
        t0 = time.monotonic()
        schema_path = extract_schema_only(backup_path)
        schema_ms = int((time.monotonic() - t0) * 1000)

        # Steps 2-5: Create temp DB, restore schema, run preflight, drop DB
        with PreflightDatabase(admin_url=admin_url) as preflight_db:
            preflight_db.restore_schema(schema_path)
            result = _run_confiture_preflight(
                confiture_config=confiture_config,
                migrations_dir=migrations_dir,
                against_url=preflight_db.url,
                timeout_seconds=timeout_seconds,
            )

        total_ms = int((time.monotonic() - start) * 1000)
        return MigrationPreflightResult(
            migrations=result.migrations,
            schema_extraction_ms=schema_ms,
            total_ms=total_ms,
        )
    finally:
        # Clean up schema temp file
        if schema_path is not None:
            try:
                schema_path.unlink(missing_ok=True)
                # Also remove the temp dir if we created it
                if schema_path.parent.name.startswith("fraisier_preflight_"):
                    schema_path.parent.rmdir()
            except OSError:
                pass

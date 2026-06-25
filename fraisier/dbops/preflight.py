"""Migration preflight: schema extraction, temp DB lifecycle, confiture integration.

Provides the building blocks for running migration preflight checks before a
full pg_restore:

1. ``extract_schema_only`` — extract schema-only SQL from a pg_dump backup
2. ``PreflightDatabase`` — create/destroy a temporary preflight database
3. ``MigrationCheck`` / ``MigrationPreflightResult`` — structured result types
4. ``run_migration_preflight`` — orchestrate the full preflight flow
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
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

    def _conn_flags(self) -> tuple[list[str], dict[str, str] | None]:
        """Build psql/pg_restore connection flags + env for this DB."""
        from fraisier.dbops.operations import _parse_connection_flags

        conn_flags, extra_env = _parse_connection_flags(self.url)
        parsed = urlparse(self.url)
        db = parsed.path.lstrip("/")
        if db:
            conn_flags = [*conn_flags, "-d", db]
        run_env = {**os.environ, **extra_env} if extra_env else None
        return conn_flags, run_env

    def restore_tracking_data(self, backup_path: Path, tracking_table: str) -> None:
        """Load the backup's migration-tracking rows into this preflight DB.

        A schema-only restore creates the tracking table empty, so the preflight
        DB cannot tell which migrations the backup already had applied.  Loading
        just the tracking table's data makes the preflight DB *self-consistent*:
        its schema and its applied-migration record both reflect the backup, so
        pending-migration detection matches what ``migrate up`` would do after a
        real restore (issue #250).

        Best-effort: a backup with no tracking table (a DB never managed by
        confiture) is tolerated — the table simply stays absent/empty and every
        migration is treated as pending by the caller.
        """
        conn_flags, run_env = self._conn_flags()
        subprocess.run(
            [
                "pg_restore",
                *conn_flags,
                "--data-only",
                "--no-owner",
                f"--table={tracking_table}",
                str(backup_path),
            ],
            check=False,  # missing tracking table / no rows is not an error
            capture_output=True,
            text=True,
            env=run_env,
        )

    def has_table(self, table_name: str) -> bool:
        """Return ``True`` if *table_name* resolves to a relation in this DB."""
        conn_flags, run_env = self._conn_flags()
        result = subprocess.run(
            [
                "psql",
                *conn_flags,
                "-tAc",
                f"SELECT to_regclass('{table_name}') IS NOT NULL",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=run_env,
        )
        return result.returncode == 0 and result.stdout.strip() == "t"

    def _scalar_int(self, sql: str) -> int | None:
        """Run *sql* expecting a single integer; ``None`` on any failure."""
        conn_flags, run_env = self._conn_flags()
        result = subprocess.run(
            ["psql", *conn_flags, "-tAc", sql],
            check=False,
            capture_output=True,
            text=True,
            env=run_env,
        )
        out = result.stdout.strip()
        if result.returncode != 0 or not out.isdigit():
            return None
        return int(out)

    def count_table_rows(self, table_name: str) -> int | None:
        """Return the row count of *table_name*, or ``None`` if it can't be read.

        Used to tell a *populated* migration ledger (the backup recorded which
        migrations are applied) from an *empty* one — the signal the #262 guard
        keys off.
        """
        if not _SAFE_IDENTIFIER.match(table_name):
            return None
        return self._scalar_int(f"SELECT count(*) FROM {table_name}")

    def count_user_relations(self, exclude_table: str) -> int | None:
        """Count user tables/views in this DB, excluding *exclude_table*.

        A schema-only restore of a real production backup recreates the
        application's tables and views; a brand-new (never-migrated) database
        has none.  This distinguishes a *populated schema with an empty ledger*
        (a failed tracking-data restore → #262) from a *genuinely fresh* backup
        (legitimately empty ledger, nothing to collide with).
        """
        base = exclude_table.rsplit(".", 1)[-1].replace("'", "''")
        return self._scalar_int(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            f"AND table_name <> '{base}'"
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
    migrations_checked: int = 0

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


_DEFAULT_TRACKING_TABLE = "tb_confiture"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.$]*$")


def _read_tracking_table(confiture_config: Path) -> str:
    """Best-effort read of ``migration.tracking_table`` from a confiture config.

    Falls back to confiture's own default (``tb_confiture``) when the file is
    missing, unparseable, omits the key, or names something that is not a safe
    SQL identifier (the value is interpolated into admin SQL / ``pg_restore``).
    """
    try:
        import yaml

        data = yaml.safe_load(confiture_config.read_text())
    except Exception:
        return _DEFAULT_TRACKING_TABLE
    if isinstance(data, dict):
        migration = data.get("migration")
        if isinstance(migration, dict):
            candidate = migration.get("tracking_table")
            if isinstance(candidate, str) and _SAFE_IDENTIFIER.match(candidate):
                return candidate
    return _DEFAULT_TRACKING_TABLE


def _write_preflight_config(
    preflight_url: str, tracking_table: str, dest_dir: Path
) -> Path:
    """Write a minimal confiture config that points pending-migration detection
    at the self-consistent preflight DB (issue #250).

    Only ``database_url`` + ``migration.tracking_table`` are needed by
    confiture's ``find_pending``.  Written ``0o600`` since the URL may carry
    credentials.
    """
    cfg = dest_dir / "preflight_confiture.yaml"
    cfg.write_text(
        f"database_url: {preflight_url}\n"
        f"migration:\n  tracking_table: {tracking_table}\n"
    )
    cfg.chmod(0o600)
    return cfg


_SKIP_PREFLIGHT_HINT = (
    "If this is a false positive (e.g. a migration that reads sibling repo "
    "files only present in a full checkout), re-run the restore with "
    "--skip-preflight to bypass the check."
)


def _extract_preflight_issues(stdout: str) -> str | None:
    """Pull human-readable issue lines from confiture's JSON preflight output.

    Reads confiture's 0.30+ ``{"issues": [...]}`` schema (an array of
    ``{severity, code, message, migration, details.error}``). Returns ``None``
    when *stdout* is not the expected JSON or carries no surfaced issues.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    lines: list[str] = []
    for issue in data.get("issues") or []:
        severity = issue.get("severity", "")
        code = issue.get("code", "")
        message = issue.get("message", "")
        migration = issue.get("migration")
        error = (issue.get("details") or {}).get("error")
        text = " ".join(p for p in (severity, code, message) if p)
        if migration:
            text = f"{text} [migration {migration}]"
        if error:
            text = f"{text} — {error}"
        if text:
            lines.append(f"  - {text}")
    return "\n".join(lines) if lines else None


def _format_preflight_failure(returncode: int, stdout: str, stderr: str) -> str:
    """Build a diagnostic message from a fatal confiture preflight exit.

    confiture writes its structured failure report (issue code, offending
    migration, the underlying error/path) to *stdout* as JSON. fraisier
    historically surfaced only *stderr* — typically empty for these failures —
    leaving operators with a bare ``exit N`` and no cause (#259). This extracts
    the human detail from stdout when present, falls back to the raw stdout,
    includes any stderr, and always appends the recovery hint.
    """
    lines = [f"confiture preflight failed (exit {returncode})."]
    detail = _extract_preflight_issues(stdout)
    if detail:
        lines.append(detail)
    elif stdout.strip():
        lines.append(stdout.strip()[:2000])
    if stderr.strip():
        lines.append(f"stderr: {stderr.strip()[:1000]}")
    lines.append(_SKIP_PREFLIGHT_HINT)
    return "\n".join(lines)


def _confiture_version() -> str | None:
    """Installed ``fraiseql-confiture`` version, or ``None`` if undiscoverable."""
    import importlib.metadata as _md

    try:
        return _md.version("fraiseql-confiture")
    except _md.PackageNotFoundError:
        return None


# A non-transactional statement (CREATE INDEX CONCURRENTLY, ALTER TYPE … ADD
# VALUE, …) cannot run inside the SAVEPOINT confiture's preflight uses, so it is
# skipped rather than replayed (`migrate up` runs it for real in autocommit).
# confiture 0.33 surfaces such a migration as a PFLIGHT_NON_TRANSACTIONAL
# warning and skips it (exit 0, confiture #169) — fraisier records the skip so a
# later dependent failure reads as a possible false positive, not a hard block.
_NON_TXN_CODE = "PFLIGHT_NON_TRANSACTIONAL"
_MIGRATION_NAME_RE = re.compile(r"Migration \S+ \(([^)]+)\)")


def _name_from_issue_message(message: str) -> str:
    """Extract the migration name from a confiture issue message.

    0.32 issue messages read ``"Migration <version> (<name>) …"``; returns the
    captured name, or ``"?"`` when the message does not match.
    """
    m = _MIGRATION_NAME_RE.search(message or "")
    return m.group(1) if m else "?"


def _parse_against_issues(data: dict) -> MigrationPreflightResult | None:
    """Map confiture 0.33 ``preflight --against`` JSON to a structured result.

    confiture emits ``{"ok", "summary": {"migrations_checked": N}, "issues":
    [...]}``. A ``PFLIGHT_NON_TRANSACTIONAL`` warning records a *skipped*
    migration; every other error-severity issue becomes a failed
    ``MigrationCheck``.

    Returns ``None`` when an error-severity issue cannot be tied to a migration
    (a structural/config error), signalling the caller to raise full diagnostics
    rather than report a bare, unmappable failure.
    """
    summary = data.get("summary") or {}
    checked = summary.get("migrations_checked", 0)
    issues = data.get("issues") or []

    checks: list[MigrationCheck] = []
    skipped_versions: set[str] = set()

    # Pass 1: non-transactional migrations are skipped during preflight.
    for issue in issues:
        version = issue.get("migration")
        if issue.get("code") == _NON_TXN_CODE and version:
            if version not in skipped_versions:
                checks.append(
                    MigrationCheck(
                        version=version,
                        name=_name_from_issue_message(issue.get("message", "")),
                        passed=True,
                        skipped=True,
                        skipped_reason=issue.get("message"),
                    )
                )
                skipped_versions.add(version)

    # Pass 2: error-severity issues are real failures.
    for issue in issues:
        if issue.get("severity") != "error":
            continue
        version = issue.get("migration")
        if version in skipped_versions:
            continue
        if not version:
            return None
        error = (issue.get("details") or {}).get("error") or issue.get("message", "")
        checks.append(
            MigrationCheck(
                version=version,
                name=_name_from_issue_message(issue.get("message", "")),
                passed=False,
                error=error,
            )
        )

    return MigrationPreflightResult(migrations=checks, migrations_checked=checked)


def _run_confiture_preflight(
    confiture_config: Path | None,
    migrations_dir: Path,
    against_url: str,
    *,
    since: str | None = None,
    timeout_seconds: int = 120,
) -> MigrationPreflightResult:
    """Run ``confiture migrate preflight --against`` via subprocess.

    Invokes the confiture CLI with ``--format json`` so the output can be
    parsed into a structured ``MigrationPreflightResult``.  With ``--against``,
    confiture 0.32 exits 0 when all replays pass and 7 when one or more fail —
    both valid outcomes that are parsed.  Any other exit code (validation,
    connection, config) indicates a fatal error.

    Args:
        confiture_config: Config file used to resolve the pending set (its
            tracking table is queried).  Pass the preflight DB's own config so
            pending detection matches the restored backup; ``None`` to fall back
            to ``since``/all-files resolution.
        migrations_dir: Directory containing migration files.
        against_url: PostgreSQL URL of the temporary preflight database.
        since: When set (and ``confiture_config`` is ``None``), resolve pending
            as every migration with version >= this value — used when the backup
            carries no tracking table, so every migration is pending.
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
        "--migrations-dir",
        str(migrations_dir),
        "--format",
        "json",
    ]
    if confiture_config is not None:
        cmd += ["--config", str(confiture_config)]
    if since is not None:
        cmd += ["--since", since]
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )

    # With --against, confiture 0.32 exits 0 = all replays passed, 7 = one or
    # more replay failures — both valid, structured outcomes to parse. Any other
    # code (2 validation, 3 connection, 5 config, …) is a fatal crash: surface
    # confiture's own diagnostics, which it writes to *stdout* (#259).
    if result.returncode not in (0, 7):
        raise DatabaseError(
            _format_preflight_failure(result.returncode, result.stdout, result.stderr)
        )

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise DatabaseError(
            _format_preflight_failure(result.returncode, result.stdout, result.stderr)
        ) from exc

    # 0.32 schema: {"ok", "summary", "issues": [...]}. A missing summary/issues
    # means a skewed confiture (e.g. the old 0.9.x against.migrations format):
    # fraisier must NOT silently treat it as "no failures", which would hide real
    # preflight failures under a version skew (#259, #262).
    if not isinstance(data, dict) or "summary" not in data or "issues" not in data:
        detail = (
            _extract_preflight_issues(result.stdout) or result.stdout.strip()[:2000]
        )
        raise DatabaseError(
            "confiture preflight returned an unrecognized output schema "
            f"(fraiseql-confiture {_confiture_version() or 'unknown'}); fraisier "
            "expects the confiture 0.30+ preflight format ({ok, summary, issues}). "
            "This is a version skew — pin fraiseql-confiture to a supported "
            f"version.\n{detail}\n{_SKIP_PREFLIGHT_HINT}"
        )

    parsed = _parse_against_issues(data)
    if parsed is None:
        # exit 7 carried an error-severity issue not tied to a migration
        # (structural/config): not representable per-migration — surface the
        # raw diagnostics rather than report a bare, unmappable failure.
        raise DatabaseError(
            _format_preflight_failure(result.returncode, result.stdout, result.stderr)
        )

    return parsed


def _guard_against_empty_ledger(
    preflight_db: PreflightDatabase, tracking_table: str
) -> None:
    """Refuse to preflight a populated schema whose ledger restored empty (#262).

    The preflight DB is a schema-only restore of the backup plus a data-only
    restore of *tracking_table*.  When the table exists (the backup was
    confiture-managed) and the restored schema already holds application
    objects, but the tracking ledger is empty, confiture would treat the entire
    history as pending and replay every already-applied migration against the
    already-populated schema — a cascade of false ``already exists`` /
    dependency failures (issue #262).  That is a failed tracking-data restore,
    not a real preflight failure, so surface it precisely instead.

    No-ops when the ledger has rows (the #250 happy path), when the schema is
    genuinely empty (a fresh, never-migrated backup — replaying all is safe),
    or when either count cannot be read (stay out of the way).
    """
    from fraisier.errors import DatabaseError

    if preflight_db.count_table_rows(tracking_table) != 0:
        return
    if (preflight_db.count_user_relations(tracking_table) or 0) == 0:
        return
    raise DatabaseError(
        "Migration preflight aborted: the backup's confiture tracking table "
        f"({tracking_table}) restored with 0 rows, but the restored schema "
        "already contains application objects. Replaying the full migration "
        "history against it would produce false 'already exists' failures "
        "(issue #262) — the backup's migration ledger did not restore. Verify "
        "the backup contains tracking-table data and that the bundled confiture "
        "matches the source database. If this backup genuinely has no applied "
        "migrations, re-run the restore with --skip-preflight."
    )


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
    4. Load the backup's migration-tracking rows so the preflight DB is
       *self-consistent* — its schema and its applied-migration record both
       reflect the backup (issue #250).
    5. Resolve the pending set from the preflight DB itself (not the live
       config DB) and run ``confiture migrate preflight --against <temp_db>``
       to test every pending migration inside a SAVEPOINT (always rolled back).
    6. Drop the temporary database and return a ``MigrationPreflightResult``.

    Resolving pending against the preflight DB is what fixes #250: the pending
    set then matches what ``migrate up`` would apply after a real restore, so a
    migration that the *live* DB already considers applied but that is absent
    from an older backup is no longer wrongly excluded (which used to make a
    later dependent migration fail against a base missing its object).

    The original database is **never touched**.  The temporary database is
    always dropped, even when a step raises an exception.

    Args:
        backup_path: Path to the pg_dump backup file (custom format).
        admin_url: PostgreSQL admin URL (maintenance DB, e.g.
            ``postgresql://admin@localhost/postgres``).
        confiture_config: Path to the confiture config file (read only for the
            migration tracking-table name).
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
    preflight_cfg: Path | None = None
    try:
        # Step 1: Extract schema
        t0 = time.monotonic()
        schema_path = extract_schema_only(backup_path)
        schema_ms = int((time.monotonic() - t0) * 1000)

        tracking_table = _read_tracking_table(confiture_config)

        # Steps 2-6: Create temp DB, restore schema + tracking, run preflight.
        with PreflightDatabase(admin_url=admin_url) as preflight_db:
            preflight_db.restore_schema(schema_path)
            preflight_db.restore_tracking_data(backup_path, tracking_table)

            if not preflight_db.has_table(tracking_table):
                # Backup predates/omits confiture tracking → no migration is
                # recorded as applied, and with no tracking table there are no
                # migration-managed objects to collide with, so every migration
                # is genuinely pending.
                result = _run_confiture_preflight(
                    confiture_config=None,
                    migrations_dir=migrations_dir,
                    against_url=preflight_db.url,
                    since="00000000000000",
                    timeout_seconds=timeout_seconds,
                )
            else:
                _guard_against_empty_ledger(preflight_db, tracking_table)
                # Backup carries confiture tracking → resolve pending from the
                # self-consistent preflight DB, matching a real restore (#250).
                preflight_cfg = _write_preflight_config(
                    preflight_db.url, tracking_table, schema_path.parent
                )
                result = _run_confiture_preflight(
                    confiture_config=preflight_cfg,
                    migrations_dir=migrations_dir,
                    against_url=preflight_db.url,
                    timeout_seconds=timeout_seconds,
                )

        total_ms = int((time.monotonic() - start) * 1000)
        return MigrationPreflightResult(
            migrations=result.migrations,
            schema_extraction_ms=schema_ms,
            total_ms=total_ms,
            migrations_checked=result.migrations_checked,
        )
    finally:
        # Clean up temp files (preflight config first — it lives in the schema
        # temp dir, which we rmdir afterwards).
        if preflight_cfg is not None:
            with contextlib.suppress(OSError):
                preflight_cfg.unlink(missing_ok=True)
        if schema_path is not None:
            try:
                schema_path.unlink(missing_ok=True)
                # Also remove the temp dir if we created it
                if schema_path.parent.name.startswith("fraisier_preflight_"):
                    schema_path.parent.rmdir()
            except OSError:
                pass

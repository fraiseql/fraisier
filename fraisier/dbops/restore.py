"""Staging restore: three-phase pg_restore (via confiture) with ownership fix
and table validation.

Restores a production backup into a staging database using confiture's
:class:`~confiture.core.restorer.DatabaseRestorer`, which runs a three-phase
restore and — crucially — holds every materialized-view ``REFRESH`` out of the
restore phases, runs a post-load ``ANALYZE``, then refreshes the matviews on
real statistics.  A stats-sensitive matview otherwise replans into a
catastrophic nested loop on the empty ``pg_statistic`` of a freshly loaded
database, turning a restore into a multi-hour hang (confiture #172,
a downstream incident).  Ownership is reassigned afterwards and the caller
validates the restore by checking the table count against a minimum threshold.
"""

import dataclasses
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from confiture.core.restorer import DatabaseRestorer, RestoreOptions
from confiture.exceptions import RestoreError

from fraisier.dbops._pgconn import PgConn
from fraisier.dbops._url import replace_db_name
from fraisier.dbops._validation import validate_file_path, validate_pg_identifier
from fraisier.dbops.backup import is_lean_dump
from fraisier.dbops.operations import _pg_cmd
from fraisier.dbops.tviews import (
    TviewError,
    TviewRebuilt,
    list_tviews,
    rebuild_all_tviews,
    rebuild_empty_tviews,
    tviews_installed,
)

if TYPE_CHECKING:
    from fraisier.dbops.archive import ArchiveCheck


@dataclass
class RestoreResult:
    """Result of a restore operation.

    ``matviews_deferred`` / ``matviews_refreshed`` / ``analyze_ran`` surface
    confiture's deferred-matview accounting (confiture #172) so the deploy log
    can show the restore phase breakdown.  ``matviews_*`` are ``None`` when the
    backup carried no materialized views — in that case confiture takes the
    classic three-phase path and ``analyze_ran`` stays ``False``.

    ``stage`` names the step that failed, so the caller stops blaming
    ``pg_restore`` for a failure in a step that runs after it — the sentence an
    operator reads at 02:00, when the service is already stopped and the
    database already dropped and reloaded (#380).
    """

    success: bool
    error: str = ""
    stage: str = "restore"
    duration_seconds: float = 0.0
    matviews_deferred: int | None = None
    matviews_refreshed: int | None = None
    analyze_ran: bool = False
    #: What ``pg_tviews_rebuild_all(only_empty)`` filled after the restore: ``()``
    #: when nothing was empty, ``None`` when the database has no pg_tviews (#422).
    tviews_rebuilt: tuple[TviewRebuilt, ...] | None = None
    #: The archive left TVIEW rows out (``tview_data: exclude``), so every TVIEW
    #: was refilled from its view rather than only the reset ones (#471).
    tviews_refilled_lean: bool = False


def _restore_connection(connection_url: str) -> PgConn:
    """The URL as libpq reads it, if confiture's :class:`RestoreOptions` can say it.

    ``RestoreOptions`` takes ``host`` / ``port`` / ``username`` / ``password``
    as discrete fields and has no connection-URL entry point, so a URL with
    more than one host cannot be expressed (it passes exactly one
    ``-h``/``-p``) and is refused. Whatever else the URL sets (``PGSSLMODE``
    and the like) reaches the restore through :meth:`PgConn.child_env`.
    """
    conn = PgConn.parse(connection_url)
    refuse_multi_host(connection_url)
    return conn


def refuse_multi_host(connection_url: str) -> None:
    """Refuse a URL naming more than one host: a restore needs exactly one.

    Raises:
        ValueError: *connection_url* names several hosts, or ``PgConn``
            refuses it.
    """
    host = PgConn.parse(connection_url).host
    if host and "," in host:
        msg = "a URL naming more than one host cannot be restored into"
        raise ValueError(msg)


def _reassign_owner(
    db_name: str,
    db_owner: str,
    *,
    connection_url: str,
) -> tuple[int, str, str]:
    """Hand every object ``CURRENT_USER`` owns in *db_name* to *db_owner*.

    Bound through stdin, not ``-c``. ``psql -c`` hands its string to the server
    unlexed, so psql never sees ``:"owner"`` and never substitutes it: every
    configured ``restore.target_owner`` failed with ``syntax error at or near
    ":"`` — after the service was stopped, the database dropped and the backup
    restored. Measured against psql 15, 16 and 18; it is not a version thing,
    ``-c`` has never substituted (#380).

    ``ON_ERROR_STOP=1`` because ``-f`` exits 0 on a failed statement.
    """
    return _pg_cmd(
        [
            "psql",
            "-d",
            db_name,
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            f"owner={db_owner}",
            "-f",
            "-",
        ],
        connection_url=connection_url,
        input_text='REASSIGN OWNED BY CURRENT_USER TO :"owner";\n',
    )


def restore_backup(
    *,
    backup_path: str,
    db_name: str,
    connection_url: str,
    db_owner: str | None = None,
    jobs: int = 1,
    min_tables: int = 0,
    min_tables_schema: str = "public",
    archive: ArchiveCheck | None = None,
) -> RestoreResult:
    """Restore a pg_dump backup into *db_name* via confiture's three-phase restore.

    Materialized-view refreshes are held out of the restore phases and run after
    a post-load ``ANALYZE`` so each matview replans on real statistics instead of
    the empty ``pg_statistic`` of a freshly loaded database (confiture #172).
    Optionally reassigns ownership to *db_owner* after the restore.

    *min_tables* is confiture's post-restore table-count floor; ``0`` means no
    floor is configured. Note what it cannot do: a dump truncated inside the
    data section restores its schema completely, so the tables exist and are
    empty and any count passes. The floor catches a restore that produced no
    schema; :func:`fraisier.dbops.archive.verify_archive`, called before the
    database is dropped, is what catches an archive whose *table of contents*
    is unreadable (#342, #343).

    Neither of them catches a truncation, and neither has to: ``pg_restore``
    does. It cannot read past the cut, writes a ``pg_restore: error:`` line and
    exits non-zero, and confiture fails the section on ``returncode != 0 and
    (exit_on_error or errors)``. That holds for ``jobs > 1`` too, where
    ``exit_on_error`` is off — the ``or errors`` half carries it, because
    tolerating a non-zero *exit* is not the same as tolerating an *error line*.
    Measured, not assumed, and pinned by
    ``tests/integration/test_restore_truncated_archive.py`` (#358).

    What none of the above can see is a restore that never ran: a database
    nobody rewrote holds stale data with entirely correct counts. That is what
    :mod:`fraisier.dbops.receipt` is for.
    """
    validate_pg_identifier(db_name, "database name")
    validate_file_path(backup_path)
    if db_owner:
        validate_pg_identifier(db_owner, "database owner")

    conn = _restore_connection(connection_url)

    options = RestoreOptions(
        backup_path=Path(backup_path),
        target_db=db_name,
        username=conn.user,
        password=conn.password,
        jobs=jobs,
        no_owner=True,
        no_acl=True,
        # jobs > 1: FK violations during the parallel data phase are transient
        # (constraints do not exist yet); parallel_restore flips confiture's
        # exit_on_error off so they do not abort the restore. jobs == 1 keeps
        # confiture's fail-fast default.
        parallel_restore=jobs > 1,
        # Forwarded, not hardcoded (#343). This read `min_tables=0` under a
        # comment saying the strategy validated the count itself after
        # `migrate up` — but that step is `if cfg.min_tables > 0` over a value
        # defaulting to 0, so in the default configuration neither side
        # validated and the comment claimed one did.
        #
        # 0 is confiture's own default and means *no floor is configured*. That
        # is a declared state, not a guarantee: the strategy says so in its log
        # rather than leaving the absence to be inferred. Both checks now run
        # at their own point — confiture's before migrations, the strategy's
        # after, where a floor can account for tables migrations create.
        min_tables=min_tables,
        # Forwarded too, and this is the half that makes a derived floor safe
        # (#343). It defaulted to confiture's `public` while the caller's count
        # could describe any schema — and #356's own host keeps its heaps in
        # `tenant`, so a floor derived from one schema and asserted against
        # another is a guaranteed false failure on a perfectly good restore.
        min_tables_schema=min_tables_schema,
        # The whole environment of every child the restore starts, and what
        # its in-process --min-tables connection reads its unset keys from:
        # the one any fraisier child gets, never the ambient libpq variables.
        # confiture refuses it beside ``superuser``, which fraisier never sets.
        env=conn.child_env(),
    )
    # confiture emits an explicit -h/-p; only override its RestoreOptions
    # defaults for the parts the URL actually carries (a socket URL with no
    # host falls through to confiture's default socket directory).
    if conn.host is not None:
        options = dataclasses.replace(options, host=conn.host)
    if conn.port is not None:
        options = dataclasses.replace(options, port=conn.port)

    t0 = time.monotonic()
    try:
        result = DatabaseRestorer().restore(options)
    except RestoreError as exc:
        return RestoreResult(
            success=False,
            stage="restore",
            error=f"restore failed: {exc}",
            duration_seconds=time.monotonic() - t0,
        )

    if not result.success:
        return RestoreResult(
            success=False,
            stage="restore",
            error=f"restore failed: {'; '.join(result.errors) or 'pg_restore failed'}",
            duration_seconds=time.monotonic() - t0,
        )

    # Fix ownership if requested
    if db_owner:
        rc, _, stderr = _reassign_owner(
            db_name, db_owner, connection_url=connection_url
        )
        if rc != 0:
            return RestoreResult(
                success=False,
                stage="reassign_owner",
                error=(
                    f"reassign_owner failed: ownership reassignment to "
                    f"{db_owner} did not run: {stderr.strip()}"
                ),
                duration_seconds=time.monotonic() - t0,
            )

    # The restored database is not the one `connection_url` names, which is
    # the maintenance database the drop/create ran from.
    restored_url = replace_db_name(connection_url, db_name)
    try:
        lean = _lean_archive(restored_url, backup_path, archive)
        if lean:
            tviews_rebuilt: tuple[TviewRebuilt, ...] | None = tuple(
                rebuild_all_tviews(restored_url)
            )
        elif tviews_installed(restored_url):
            tviews_rebuilt = tuple(rebuild_empty_tviews(restored_url))
        else:
            tviews_rebuilt = None
    except (TviewError, psycopg.Error) as exc:
        return RestoreResult(
            success=False,
            stage="tview_rebuild",
            error=f"tview_rebuild failed: the restore ran, but {exc}",
            duration_seconds=time.monotonic() - t0,
        )

    return RestoreResult(
        success=True,
        duration_seconds=time.monotonic() - t0,
        matviews_deferred=result.matviews_deferred,
        matviews_refreshed=result.matviews_refreshed,
        analyze_ran=result.analyze_ran,
        tviews_rebuilt=tviews_rebuilt,
        tviews_refilled_lean=lean,
    )


def _lean_archive(url: str, backup_path: str, archive: ArchiveCheck | None) -> bool:
    """The archive left TVIEW rows out, so ``only_empty`` would not refill them.

    ``only_empty`` refills only a TVIEW PostgreSQL *reset*.  A LOGGED one (the
    default from pg_tviews beta.28) never is, and on beta.29 an UNLOGGED one
    keeps its validity row through ``--exclude-table-data``: either way it
    would be served empty (measured, #471).

    The archive's table of contents decides when it was read and pg_tviews can
    be: a registered TVIEW the archive creates without its rows.  It is never
    "any table without rows", which a partitioned parent also is.  Otherwise
    the file name decides; a lean name implies the source had pg_tviews, since
    a dump with nothing to exclude is never named lean.  The caller then
    refills with :func:`rebuild_all_tviews`, which raises on a pg_tviews it
    cannot read rather than skipping.
    """
    if archive is not None and archive.table_entries is not None:
        if tviews_installed(url):
            return any(
                archive.has_table(schema, name)
                and not archive.has_table_data(schema, name)
                for schema, name in list_tviews(url)
            )
    return is_lean_dump(backup_path)


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

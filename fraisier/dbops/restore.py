"""Staging restore: three-phase pg_restore (via confiture) with ownership fix
and table validation.

Restores a production backup into a staging database using confiture's
:class:`~confiture.core.restorer.DatabaseRestorer`, which runs a three-phase
restore and — crucially — holds every materialized-view ``REFRESH`` out of the
restore phases, runs a post-load ``ANALYZE``, then refreshes the matviews on
real statistics.  A stats-sensitive matview otherwise replans into a
catastrophic nested loop on the empty ``pg_statistic`` of a freshly loaded
database, turning a restore into a multi-hour hang (confiture #172,
printoptim_backend#1960).  Ownership is reassigned afterwards and the caller
validates the restore by checking the table count against a minimum threshold.
"""

import contextlib
import dataclasses
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from confiture.core.restorer import DatabaseRestorer, RestoreOptions
from confiture.exceptions import RestoreError

from fraisier.dbops._validation import validate_file_path, validate_pg_identifier
from fraisier.dbops.operations import _pg_cmd


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


def _connection_params(
    connection_url: str,
) -> tuple[str | None, int | None, str | None, dict[str, str]]:
    """Split a PostgreSQL URL into ``(host, port, username, env)`` for confiture.

    confiture's :class:`RestoreOptions` takes ``host`` / ``port`` / ``username``
    as discrete fields and shells out with an explicit ``-h/-p/-U`` — it has no
    connection-URL entry point.  The host may live in the netloc
    (``postgresql://h:p/db``) or, for socket connections, in a ``?host=`` query
    parameter (``postgresql:///db?host=/var/run/postgresql``); both are mapped,
    mirroring :func:`operations._parse_connection_flags`.  A password rides back
    in ``env`` as ``PGPASSWORD`` for confiture's inherited subprocess
    environment to pick up.
    """
    parsed = urlparse(connection_url)
    query = parse_qs(parsed.query)

    def _q(key: str) -> str | None:
        values = query.get(key)
        return values[0] if values else None

    host = parsed.hostname or _q("host")
    port = parsed.port
    if port is None:
        raw_port = _q("port")
        if raw_port:
            with contextlib.suppress(ValueError):
                port = int(raw_port)
    username = parsed.username or _q("user")
    env: dict[str, str] = {}
    password = parsed.password or _q("password")
    if password:
        env["PGPASSWORD"] = password
    return host, port, username, env


@contextlib.contextmanager
def _augmented_env(extra: dict[str, str]) -> Iterator[None]:
    """Temporarily overlay *extra* onto ``os.environ`` for the duration of a call.

    confiture's restorer inherits ``os.environ`` (it never passes ``env=`` to
    its subprocesses), so a password parsed from the connection URL is threaded
    through the process environment as ``PGPASSWORD`` while the restore runs and
    then restored to its prior value.
    """
    if not extra:
        yield
        return
    saved = {key: os.environ.get(key) for key in extra}
    os.environ.update(extra)
    try:
        yield
    finally:
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


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

    host, port, username, extra_env = _connection_params(connection_url)

    options = RestoreOptions(
        backup_path=Path(backup_path),
        target_db=db_name,
        username=username,
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
    )
    # confiture emits an explicit -h/-p; only override its RestoreOptions
    # defaults for the parts the URL actually carries (a socket URL with no
    # host falls through to confiture's default socket directory).
    if host is not None:
        options = dataclasses.replace(options, host=host)
    if port is not None:
        options = dataclasses.replace(options, port=port)

    t0 = time.monotonic()
    try:
        with _augmented_env(extra_env):
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

    return RestoreResult(
        success=True,
        duration_seconds=time.monotonic() - t0,
        matviews_deferred=result.matviews_deferred,
        matviews_refreshed=result.matviews_refreshed,
        analyze_ran=result.analyze_ran,
    )


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

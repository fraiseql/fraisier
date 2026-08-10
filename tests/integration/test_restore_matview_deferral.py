"""End-to-end proof that fraisier's restore defers matview refresh past ANALYZE.

fraisier's ``restore_backup`` delegates to confiture's three-phase
``DatabaseRestorer``, which holds every ``REFRESH MATERIALIZED VIEW`` out of the
restore phases, runs ``ANALYZE``, then refreshes on real statistics
(confiture #172).  These tests exercise that seam against a *real* local
PostgreSQL: a backup carrying a populated matview is dumped, restored through
fraisier, and the matview must come out populated with the deferral accounting
reported — the unit tests only prove the options handed to confiture, not that
the two actually run together.

Skipped unless a local PostgreSQL with ``createdb`` privilege and the client
tools (``pg_dump`` / ``pg_restore``) are reachable — the shared ``socket_dir``
fixture in ``conftest.py`` decides that.
"""

from __future__ import annotations

import contextlib
import subprocess

import pytest

pytestmark = pytest.mark.integration

psycopg = pytest.importorskip("psycopg")

_SRC_DB = "fraisier_it_mv_src"
_DST_DB = "fraisier_it_mv_dst"


def _dsn(db: str, socket_dir: str) -> str:
    return f"postgresql:///{db}?host={socket_dir}"


def _exec(db: str, socket_dir: str, *statements: str) -> None:
    with psycopg.connect(_dsn(db, socket_dir), autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


def _drop_databases(socket_dir: str) -> None:
    for db in (_SRC_DB, _DST_DB):
        with contextlib.suppress(Exception):
            _exec("postgres", socket_dir, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


@pytest.mark.parametrize("jobs", [1, 4])
def test_restore_backup_populates_matview_end_to_end(socket_dir, tmp_path, jobs):
    """A restored backup with a populated matview comes out populated.

    Proves confiture's deferred-refresh path runs through fraisier for both the
    serial (``jobs=1``) and parallel (``jobs=4``) restore paths — the parallel
    case is the exact printoptim_backend#1960 incident condition (a matview
    refresh replanning on empty stats during the ``--jobs 4`` data phase).  The
    matview must end up filled (not left ``WITH NO DATA``) and the deferral
    accounting is surfaced on fraisier's RestoreResult.
    """
    from fraisier.dbops.restore import restore_backup

    dump_path = tmp_path / "src.dump"
    _drop_databases(socket_dir)
    try:
        _exec("postgres", socket_dir, f"CREATE DATABASE {_SRC_DB}")
        _exec(
            _SRC_DB,
            socket_dir,
            "CREATE TABLE t (id int PRIMARY KEY, label text)",
            "INSERT INTO t SELECT g, 'row' || g FROM generate_series(1, 500) g",
            "CREATE MATERIALIZED VIEW mv AS "
            "SELECT label, count(*) AS c FROM t GROUP BY label WITH DATA",
        )

        # confiture's three-phase restore requires custom (-Fc) or directory
        # format; a plain SQL dump is rejected.
        subprocess.run(
            ["pg_dump", "-Fc", "-h", socket_dir, "-d", _SRC_DB, "-f", str(dump_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        _exec("postgres", socket_dir, f"CREATE DATABASE {_DST_DB}")

        result = restore_backup(
            backup_path=str(dump_path),
            db_name=_DST_DB,
            connection_url=_dsn("postgres", socket_dir),
            jobs=jobs,
        )

        assert result.success is True, result.error
        # Exactly one matview in the dump → deferred out of the phases, then
        # refreshed after ANALYZE on real statistics.
        assert result.matviews_deferred == 1
        assert result.matviews_refreshed == 1
        assert result.analyze_ran is True

        with psycopg.connect(_dsn(_DST_DB, socket_dir)) as conn:
            table_rows = conn.execute("SELECT count(*) FROM t").fetchone()[0]
            matview_rows = conn.execute("SELECT count(*) FROM mv").fetchone()[0]
        assert table_rows == 500
        # Populated by the deferred refresh — not left empty.
        assert matview_rows == 500
    finally:
        _drop_databases(socket_dir)

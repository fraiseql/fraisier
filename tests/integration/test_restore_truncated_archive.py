"""A dump truncated inside its data section must fail the restore (#358).

#358 is right about the premise and this test asserts it first: a dump cut off
partway through its ``COPY`` stream still carries a complete table of contents,
so ``pg_restore --list`` reports every table, :func:`verify_archive` returns
VALID, and the floor it derives is satisfied by a database containing no rows.
Neither the archive check nor any table-count floor of any derivation can see
the damage.

What catches it is the restore itself. ``pg_restore`` cannot read past the cut,
writes a ``pg_restore: error:`` line and exits non-zero, and confiture fails the
section on ``returncode != 0 and (exit_on_error or errors)``
(``restorer.py:793``). Both halves of that disjunction matter here: ``jobs > 1``
turns ``exit_on_error`` off, because FK violations during the parallel data phase
are transient, and the restore survives on the ``or errors`` half instead —
tolerating a non-zero *exit* is not the same as tolerating an *error line*.

That is a guard on the far side of the confiture seam, keyed on a literal string
in ``pg_restore``'s stderr. It is asserted here at fraisier's own boundary, on
``restore_backup(...).success``, so a confiture bump that reclassifies those
lines fails this test rather than a nightly.

Skipped unless a PostgreSQL with ``createdb`` privilege and the client tools are
reachable — over a unix socket locally, or over TCP against CI's service
container. The shared ``pg_target`` fixture in ``conftest.py`` decides that; a
promise this test makes about a confiture bump is only kept where it runs.
"""

from __future__ import annotations

import contextlib
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.integration.conftest import PgTarget

pytestmark = pytest.mark.integration

psycopg = pytest.importorskip("psycopg")

_SRC_DB = "fraisier_it_trunc_src"
_DST_DB = "fraisier_it_trunc_dst"

#: Rows per table, sized so the data section dwarfs the table of contents and a
#: cut at any of the fractions below lands inside a ``COPY`` stream rather than
#: in the header.
_ROWS = 20_000

#: Where to cut, as a fraction of the whole dump. Three depths, because the
#: failure mode differs down the file: an early cut loses a whole block, a late
#: one truncates the final table after earlier ones have loaded intact.
_CUT_FRACTIONS = (0.5, 0.9, 0.98)


def _exec(db: str, target: PgTarget, *statements: str) -> None:
    with psycopg.connect(target.dsn(db), autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


def _drop_databases(target: PgTarget) -> None:
    for db in (_SRC_DB, _DST_DB):
        with contextlib.suppress(Exception):
            _exec("postgres", target, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


@pytest.fixture
def truncated_dumps(pg_target, tmp_path):
    """Yield ``(pg_target, {fraction: path})`` for one dump cut at each depth.

    The source database is built and dumped once; the cuts are byte slices of
    that single dump, so every case describes the same archive truncated at a
    different point.
    """
    full = tmp_path / "src.dump"
    _drop_databases(pg_target)
    try:
        _exec("postgres", pg_target, f"CREATE DATABASE {_SRC_DB}")
        _exec(
            _SRC_DB,
            pg_target,
            "CREATE TABLE t (id int PRIMARY KEY, label text)",
            f"INSERT INTO t SELECT g, repeat('x', 200) || g "
            f"FROM generate_series(1, {_ROWS}) g",
            "CREATE TABLE t2 (id int PRIMARY KEY, label text)",
            f"INSERT INTO t2 SELECT g, repeat('y', 200) || g "
            f"FROM generate_series(1, {_ROWS}) g",
        )
        # Custom format: confiture's three-phase restore rejects a plain SQL
        # dump, and only -Fc/-Fd carry the table of contents this is about.
        # The whole DSN goes in as -d, which addresses either transport and
        # carries the credentials a TCP server asks for.
        subprocess.run(
            ["pg_dump", "-Fc", "-d", pg_target.dsn(_SRC_DB), "-f", str(full)],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = full.read_bytes()
        cuts = {}
        for fraction in _CUT_FRACTIONS:
            cut = tmp_path / f"cut_{int(fraction * 100)}.dump"
            cut.write_bytes(payload[: int(len(payload) * fraction)])
            cuts[fraction] = cut
        yield pg_target, cuts
    finally:
        _drop_databases(pg_target)


@pytest.mark.parametrize("fraction", _CUT_FRACTIONS)
def test_truncated_dump_still_passes_every_check_that_precedes_the_restore(
    truncated_dumps, fraction
):
    """The premise of #358: nothing before the restore can see the truncation.

    This is the half of the issue that is correct and stays correct. It is
    asserted so that a future change which *does* let an earlier check catch a
    truncated dump is noticed here rather than assumed.
    """
    from fraisier.dbops.archive import ArchiveVerdict, verify_archive

    _, cuts = truncated_dumps
    cut = cuts[fraction]

    listed = subprocess.run(
        ["pg_restore", "--list", str(cut)], capture_output=True, text=True, check=False
    )
    assert listed.returncode == 0, listed.stderr

    check = verify_archive(cut)
    assert check.verdict is ArchiveVerdict.VALID
    assert check.is_bad is False
    # Both tables are still described by the table of contents, so the floor the
    # archive states about itself is met by the empty schema a truncated restore
    # would leave behind.
    assert check.schema_floor == ("public", 2)


@pytest.mark.parametrize("fraction", _CUT_FRACTIONS)
@pytest.mark.parametrize("jobs", [1, 4])
def test_truncated_dump_fails_the_restore(truncated_dumps, fraction, jobs):
    """The restore fails on a truncated archive, serially and in parallel.

    ``jobs=1`` keeps confiture's fail-fast ``exit_on_error``; ``jobs=4`` turns it
    off and must still fail, on the error lines rather than the exit status.
    """
    from fraisier.dbops.restore import restore_backup

    pg_target, cuts = truncated_dumps
    cut = cuts[fraction]

    with contextlib.suppress(Exception):
        _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DST_DB} WITH (FORCE)")
    _exec("postgres", pg_target, f"CREATE DATABASE {_DST_DB}")

    result = restore_backup(
        backup_path=str(cut),
        db_name=_DST_DB,
        connection_url=pg_target.dsn("postgres"),
        jobs=jobs,
        # The floor the archive states about itself — the one v0.64.0 derives and
        # enforces. Passing it here is deliberate: the restore must fail on the
        # truncation even with the floor armed, because the floor cannot see it.
        min_tables=2,
        min_tables_schema="public",
    )

    assert result.success is False
    assert result.error, "a failed restore must say why"

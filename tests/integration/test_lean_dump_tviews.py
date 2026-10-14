"""A lean dump (``tview_data: exclude``) restored through fraisier (#471).

The dump leaves every TVIEW's rows out, the archive must show it, and fraisier's
restore refills every TVIEW from its backing view, after which writes still
propagate.  ``tv_user`` is LOGGED (the default from beta.28), which
``only_empty`` never refills: the control restores the same archive without the
explicit refill and finds it empty, so the test can see the gap it closes.
``tv_post`` is UNLOGGED, so the refill covers both kinds.

Needs pg_tviews read contract 2 (0.1.0-beta.30, the release confiture 1.37
requires) on the server.  The ``tviews`` CI leg runs it; elsewhere it skips
without one.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from fraisier.dbops.archive import ArchiveCheck
    from tests.integration.conftest import PgTarget

pytestmark = pytest.mark.integration

psycopg = pytest.importorskip("psycopg")

_SRC_DB = "fraisier_it_lean_src"
_DST_DB = "fraisier_it_lean_dst"

_TREE = """
CREATE TABLE tb_user (pk_user bigint PRIMARY KEY, id uuid NOT NULL UNIQUE, name text);
CREATE TABLE tb_post (
    pk_post bigint PRIMARY KEY, id uuid NOT NULL UNIQUE,
    fk_user bigint REFERENCES tb_user, title text
);
CREATE UNLOGGED TABLE tv_post AS
SELECT p.pk_post, p.id, jsonb_build_object('id', p.id, 'author', u.name) AS data
FROM tb_post p JOIN tb_user u ON u.pk_user = p.fk_user;
CREATE TABLE tv_user AS
SELECT pk_user, id, jsonb_build_object('name', name) AS data FROM tb_user;
"""


def _exec(target: PgTarget, db: str, *statements: str) -> None:
    with psycopg.connect(target.dsn(db), autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


def _count(target: PgTarget, db: str, table: str) -> int:
    with psycopg.connect(target.dsn(db), autocommit=True) as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def lean_dump(pg_tviews_target: PgTarget, tmp_path: Path):
    from fraisier.dbops.backup import run_backup, tviews_to_exclude

    target = pg_tviews_target

    def drop() -> None:
        for db in (_SRC_DB, _DST_DB):
            with contextlib.suppress(Exception):
                _exec(target, "postgres", f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")

    drop()
    _exec(target, "postgres", f"CREATE DATABASE {_SRC_DB}")
    # Its own round trip: in one batch with CREATE EXTENSION the hook is not
    # loaded yet and the TVIEWs become plain tables.
    _exec(target, _SRC_DB, "CREATE EXTENSION pg_tviews")
    _exec(
        target,
        _SRC_DB,
        _TREE,
        "INSERT INTO tb_user SELECT g, gen_random_uuid(), 'u' || g "
        "FROM generate_series(1, 30) g",
        "INSERT INTO tb_post SELECT g, gen_random_uuid(), 1 + g % 30, 't' || g "
        "FROM generate_series(1, 200) g",
    )
    tables = tviews_to_exclude(
        "exclude", database_url=target.dsn("postgres"), db_name=_SRC_DB
    )
    assert tables == [("public", "tv_post"), ("public", "tv_user")]
    result = run_backup(
        db_name=_SRC_DB,
        output_dir=str(tmp_path),
        database_url=target.dsn(_SRC_DB),
        compression="none",
        tview_tables=tables,
    )
    assert result.success is True, result.error
    try:
        yield target, result.backup_path
    finally:
        drop()


def _restore(target: PgTarget, path: str, *, archive: ArchiveCheck | None):
    from fraisier.dbops.restore import restore_backup

    _exec(target, "postgres", f"DROP DATABASE IF EXISTS {_DST_DB} WITH (FORCE)")
    _exec(target, "postgres", f"CREATE DATABASE {_DST_DB}")
    return restore_backup(
        backup_path=path,
        db_name=_DST_DB,
        connection_url=target.dsn("postgres"),
        archive=archive,
    )


def test_the_archive_holds_no_tview_rows(lean_dump) -> None:
    from fraisier.dbops.archive import verify_archive

    _target, path = lean_dump
    check = verify_archive(path)

    assert "_lean" in path
    for name in ("tv_post", "tv_user"):
        assert check.has_table("public", name)
        assert not check.has_table_data("public", name)
    assert check.has_table_data("public", "tb_post")


def test_a_lean_restore_refills_every_tview_and_they_propagate(lean_dump) -> None:
    from fraisier.dbops import tviews
    from fraisier.dbops.archive import verify_archive

    target, path = lean_dump

    result = _restore(target, path, archive=verify_archive(path))

    assert result.success is True, result.error
    assert result.tviews_refilled_lean is True
    assert {r.entity for r in result.tviews_rebuilt or ()} == {"post", "user"}
    assert _count(target, _DST_DB, "tv_post") == 200
    assert _count(target, _DST_DB, "tv_user") == 30
    assert tviews.find_empty_tviews(target.dsn(_DST_DB)) == []
    _exec(target, _DST_DB, "UPDATE tb_user SET name = 'grace' WHERE pk_user = 1")
    with psycopg.connect(target.dsn(_DST_DB), autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM tv_post WHERE data->>'author' = 'grace'"
        ).fetchone()
    assert row is not None
    assert row[0] > 0


def test_control_only_empty_leaves_the_logged_tview_empty(
    lean_dump, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the explicit refill is for: ``only_empty`` skips a LOGGED TVIEW.

    The same lean archive, with neither its table of contents nor its name
    telling the restore it is lean.
    """
    from fraisier.dbops import tviews

    target, path = lean_dump
    monkeypatch.setattr("fraisier.dbops.restore.is_lean_dump", lambda _path: False)

    result = _restore(target, path, archive=None)

    assert result.success is True, result.error
    assert result.tviews_refilled_lean is False
    assert _count(target, _DST_DB, "tv_user") == 0
    empty = tviews.find_empty_tviews(target.dsn(_DST_DB))
    assert "public.tv_user" in {e.tview for e in empty}

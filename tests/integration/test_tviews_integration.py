"""fraisier's pg_tviews calls against a real pg_tviews (#422).

An UNLOGGED TVIEW is emptied by a crash-recovery start, a failover or a physical
restore.  ``TRUNCATE`` reproduces the state exactly as pg_tviews itself reports
it (``is_empty`` and ``needs_rebuild`` both true), without needing a cluster to
crash.

Needs pg_tviews 0.1.0-beta.20 or later; CI has none, so there this skips and the
PR records that it ran locally.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from fraisier.dbops import tviews

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.integration.conftest import PgTarget

pytestmark = pytest.mark.integration

psycopg = pytest.importorskip("psycopg")

_DB = "fraisier_it_tviews"

_TREE = """
CREATE TABLE tb_user (pk_user bigint PRIMARY KEY, id uuid NOT NULL UNIQUE, name text);
CREATE TABLE tb_post (
    pk_post bigint PRIMARY KEY, id uuid NOT NULL UNIQUE,
    fk_user bigint REFERENCES tb_user, title text
);
CREATE TABLE tv_post AS
SELECT p.pk_post, p.id, jsonb_build_object('id', p.id, 'author', u.name) AS data
FROM tb_post p JOIN tb_user u ON u.pk_user = p.fk_user;
CREATE TABLE tv_user AS
SELECT pk_user, id, jsonb_build_object('name', name) AS data FROM tb_user;
INSERT INTO tb_user VALUES (1, '00000000-0000-0000-0000-000000000001', 'ada');
INSERT INTO tb_post VALUES (1, '00000000-0000-0000-0000-0000000000a1', 1, 'first');
"""


def _count(url: str, table: str) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def url(pg_tviews_target: PgTarget) -> Iterator[str]:
    admin = pg_tviews_target.dsn("postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {_DB}")
    target = pg_tviews_target.dsn(_DB)
    try:
        # Own round trips: in one batch with CREATE EXTENSION the hook is not
        # loaded yet and `tv_post` becomes a plain table.
        with psycopg.connect(target, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION pg_tviews")
        with psycopg.connect(target, autocommit=True) as conn:
            conn.execute(_TREE)
        yield target
    finally:
        with (
            contextlib.suppress(Exception),
            psycopg.connect(admin, autocommit=True) as conn,
        ):
            conn.execute(f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")


def _empty(url: str, table: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {table}")


def test_a_healthy_database_has_nothing_empty(url: str) -> None:
    assert tviews.tviews_installed(url)
    assert tviews.find_empty_tviews(url) == []


def test_a_truncated_tview_is_found_by_name_and_view(url: str) -> None:
    _empty(url, "tv_post")

    (found,) = tviews.find_empty_tviews(url)

    assert (found.tview, found.view) == ("public.tv_post", "public.v_post")


def test_rebuild_brings_the_rows_back_and_names_the_entity(url: str) -> None:
    _empty(url, "tv_post")

    rebuilt = tviews.rebuild_empty_tviews(url)

    assert rebuilt == [tviews.TviewRebuilt("post", 1)]
    assert _count(url, "tv_post") == 1
    assert tviews.find_empty_tviews(url) == []


def test_only_empty_leaves_a_healthy_tview_alone(url: str) -> None:
    """Two TVIEWs, one emptied: only that one is rebuilt.

    Mutation: with ``only_empty`` forced to false this returns both entities.
    """
    _empty(url, "tv_post")

    rebuilt = tviews.rebuild_empty_tviews(url)

    assert [r.entity for r in rebuilt] == ["post"]


def test_rebuild_all_rebuilds_a_healthy_tview_too(url: str) -> None:
    rebuilt = tviews.rebuild_all_tviews(url)

    assert sorted(r.entity for r in rebuilt) == ["post", "user"]


def test_a_database_without_pg_tviews_is_unaffected(pg_tviews_target: PgTarget) -> None:
    admin = pg_tviews_target.dsn("postgres")
    plain = "fraisier_it_tviews_plain"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {plain} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {plain}")
    try:
        target = pg_tviews_target.dsn(plain)
        assert tviews.tviews_installed(target) is False
        assert tviews.find_empty_tviews(target) == []
        assert tviews.rebuild_empty_tviews(target) == []
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {plain} WITH (FORCE)")

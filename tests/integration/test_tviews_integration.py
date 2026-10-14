"""fraisier's pg_tviews calls against a real pg_tviews (#422).

An UNLOGGED TVIEW is emptied by a crash-recovery start, a failover or a physical
restore.  :func:`_empty` reproduces the state exactly as pg_tviews itself reports
it (``is_empty`` and ``needs_rebuild`` both true), without needing a cluster to
crash.

Needs pg_tviews read contract 2 (0.1.0-beta.30, the release confiture 1.37
requires).  The ``tviews`` CI leg runs it; elsewhere it skips without one.
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
    """Leave *table* as a reset empties it.

    A reset TVIEW is an UNLOGGED one missing from ``tviews.pg_tview_valid``, the
    UNLOGGED table a reset empties with it.  A TVIEW that is merely empty is not
    rebuilt by ``only_empty`` (from beta.27).

    beta.28 keys that table by the TVIEW's OID, beta.29 by its entity
    (pg_tviews#236), so the key is read from the table rather than assumed.
    """
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"ALTER TABLE {table} SET UNLOGGED")
        conn.execute(f"TRUNCATE {table}")
        key = conn.execute(
            "SELECT attname FROM pg_attribute "
            "WHERE attrelid = 'tviews.pg_tview_valid'::regclass "
            "AND attname IN ('table_oid', 'entity') AND NOT attisdropped"
        ).fetchall()
        if key == [("entity",)]:
            conn.execute(
                "DELETE FROM tviews.pg_tview_valid WHERE entity IN ("
                "SELECT entity FROM tviews.registry "
                "WHERE format('%%I.%%I', schema, name)::regclass = %s::regclass)",
                (table,),
            )
        elif key == [("table_oid",)]:
            conn.execute(
                "DELETE FROM tviews.pg_tview_valid WHERE table_oid = %s::regclass",
                (table,),
            )
        else:
            msg = f"tviews.pg_tview_valid has no key this helper knows: {key}"
            raise AssertionError(msg)


def test_a_healthy_database_has_nothing_empty(url: str) -> None:
    assert tviews.tviews_installed(url)
    assert tviews.find_empty_tviews(url) == []


def test_a_truncated_tview_is_found_by_name_and_view(url: str) -> None:
    _empty(url, "tv_post")

    (found,) = tviews.find_empty_tviews(url)

    # The view is wherever the registry says: `public.v_post` before pg_tviews
    # 0.1.0-beta.25, `tviews.public__tv_post` from it. The probe must agree with
    # the registry, never with a name this test infers.
    with psycopg.connect(url, autocommit=True) as conn:
        (expected,) = conn.execute(
            "SELECT format('%s.%s', n.nspname, c.relname) "
            "FROM tviews.registry r JOIN pg_class c ON c.oid = r.view "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE r.schema = 'public' AND r.name = 'tv_post'"
        ).fetchone()
    assert (found.tview, found.view) == ("public.tv_post", expected)


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


#: pg_tviews 0.1.0-beta.27 stops granting its bulk maintenance functions to
#: ``PUBLIC`` (#460).  Revoking it here keeps the fixture independent of the
#: extension's defaults.
_REBUILDER = "fraisier_it_rebuilder"
_OWNER = "fraisier_it_tview_owner"


def _as_role(url: str, role: str) -> str:
    """*url*, connecting as itself and then acting as *role* (``SET ROLE``).

    A superuser that sets a role loses superuser for privilege checks, which is
    what a non-superuser restore role is, without a password or a socket mapping.
    """
    return f"{url}{'&' if '?' in url else '?'}options=-c%20role%3D{role}"


@pytest.fixture
def without_public_rebuild(url: str) -> Iterator[str]:
    """*url* as a role with no privilege on a TVIEW and none on the rebuild.

    pg_tviews rebuilds and counts each TVIEW as its owner (from beta.27), so
    ``EXECUTE`` is the only thing this role is missing.
    """
    with psycopg.connect(url, autocommit=True) as conn:
        # Roles are cluster-wide; its grants die with the fixture's database.
        if not conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (_REBUILDER,)
        ).fetchone():
            conn.execute(f"CREATE ROLE {_REBUILDER}")
        conn.execute(
            "REVOKE EXECUTE ON FUNCTION tviews.pg_tviews_rebuild_all(boolean) "
            "FROM PUBLIC"
        )
    yield _as_role(url, _REBUILDER)


def test_a_role_without_execute_is_told_the_grant_before_any_restore(
    without_public_rebuild: str,
) -> None:
    problem = tviews.rebuild_privilege_problem(without_public_rebuild)

    assert problem is not None
    assert (
        f'GRANT EXECUTE ON FUNCTION "tviews".pg_tviews_rebuild_all(boolean) '
        f'TO "{_REBUILDER}"'
    ) in problem


def test_the_grant_it_names_is_the_fix(url: str, without_public_rebuild: str) -> None:
    """The statement in the message is executed verbatim, then the rebuild runs
    for a role that has nothing on the TVIEWs themselves."""
    problem = tviews.rebuild_privilege_problem(without_public_rebuild)
    assert problem is not None
    grant = problem[problem.index("GRANT") :].split(";")[0]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(grant)
    _empty(url, "tv_post")

    assert tviews.rebuild_privilege_problem(without_public_rebuild) is None
    assert tviews.rebuild_empty_tviews(without_public_rebuild) == [
        tviews.TviewRebuilt("post", 1)
    ]


def test_a_refused_rebuild_names_the_grant_not_just_the_sqlstate(
    without_public_rebuild: str,
) -> None:
    with pytest.raises(tviews.TviewError) as excinfo:
        tviews.rebuild_empty_tviews(without_public_rebuild)

    assert "permission denied for function pg_tviews_rebuild_all" in str(excinfo.value)
    assert (
        f'GRANT EXECUTE ON FUNCTION "tviews".pg_tviews_rebuild_all(boolean) '
        f'TO "{_REBUILDER}"'
    ) in str(excinfo.value)


def test_a_superuser_has_no_privilege_problem(url: str) -> None:
    assert tviews.rebuild_privilege_problem(url) is None


def test_a_database_without_pg_tviews_has_no_privilege_problem(
    pg_tviews_target: PgTarget,
) -> None:
    assert tviews.rebuild_privilege_problem(pg_tviews_target.dsn("postgres")) is None


def test_a_refusal_on_a_table_names_the_owner_not_the_function_grant(
    url: str,
) -> None:
    """With ``EXECUTE`` granted, a ``42501`` is the TVIEW owner's.

    pg_tviews rebuilds each TVIEW as its owner (from beta.27), so a superuser is
    refused the same way (measured): naming the function grant, or a superuser,
    would be a false fix.  The owner here has lost ``SELECT`` on the table its
    TVIEW reads.
    """
    with psycopg.connect(url, autocommit=True) as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (_OWNER,)
        ).fetchone():
            conn.execute(f"CREATE ROLE {_OWNER}")
        conn.execute(f"GRANT CREATE ON SCHEMA public TO {_OWNER}")
        conn.execute(f"SET ROLE {_OWNER}")
        conn.execute(
            "CREATE TABLE tb_note (pk_note bigint PRIMARY KEY, id uuid NOT NULL "
            "UNIQUE, body text)"
        )
        conn.execute(
            "INSERT INTO tb_note VALUES "
            "(1, '00000000-0000-0000-0000-0000000000b1', 'hi')"
        )
        conn.execute(
            "CREATE TABLE tv_note AS SELECT pk_note, id, "
            "jsonb_build_object('body', body) AS data FROM tb_note"
        )
        conn.execute(f"REVOKE SELECT ON tb_note FROM {_OWNER}")
        conn.execute("RESET ROLE")
    _empty(url, "tv_note")

    with pytest.raises(tviews.TviewError) as excinfo:
        tviews.rebuild_empty_tviews(url)

    message = str(excinfo.value)
    assert "permission denied for table tb_note" in message
    assert "owner" in message
    assert "GRANT EXECUTE" not in message
    assert "superuser" not in message

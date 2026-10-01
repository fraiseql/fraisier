"""A TVIEW restored through fraisier still follows its base tables (#422).

``pg_restore --section=data --disable-triggers`` keeps the source database's
OIDs, and pg_tviews rebinds the OIDs it recorded in a trigger as ``pg_tview_meta``
loads — so the restored TVIEW is registered, counts correctly, and silently stops
following its base tables.  confiture's restorer never passes the flag.

Measured, not assumed: the same test passes on confiture 1.26.0 and 1.29.0
against pg_tviews 0.1.0-beta.20, so raising the cap did **not** close the restore
half of #422 — what that half depends on is the pg_tviews version on the host
(``fraisier doctor``'s ``pg_tviews_contract`` check).  The mutation was measured
too: restoring by hand with ``--disable-triggers`` leaves ``tv_post`` at the old
value, so this assertion can see a dead TVIEW.  This test is the regression pin
for either side of that changing.

Needs pg_tviews 0.1.0-beta.20 or later on the server.  CI has none, so there
this skips — the ``pg_tviews_target`` fixture says so — and the PR records that
it ran locally.
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

_SRC_DB = "fraisier_it_tv_src"
_DST_DB = "fraisier_it_tv_dst"

_TREE = """
CREATE TABLE tb_user (pk_user bigint PRIMARY KEY, id uuid NOT NULL UNIQUE, name text);
CREATE TABLE tb_post (
    pk_post bigint PRIMARY KEY, id uuid NOT NULL UNIQUE,
    fk_user bigint REFERENCES tb_user, title text
);
CREATE TABLE tv_post AS
SELECT p.pk_post, p.id,
       jsonb_build_object('id', p.id, 'author', u.name) AS data
FROM tb_post p JOIN tb_user u ON u.pk_user = p.fk_user;
"""


def _exec(db: str, target: PgTarget, *statements: str) -> None:
    with psycopg.connect(target.dsn(db), autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


def _drop_databases(target: PgTarget) -> None:
    for db in (_SRC_DB, _DST_DB):
        with contextlib.suppress(Exception):
            _exec("postgres", target, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


@pytest.mark.parametrize("jobs", [1, 4])
def test_a_restored_tview_follows_its_base_tables(pg_tviews_target, tmp_path, jobs):
    from fraisier.dbops.restore import restore_backup

    target = pg_tviews_target
    dump_path = tmp_path / "src.dump"
    _drop_databases(target)
    try:
        _exec("postgres", target, f"CREATE DATABASE {_SRC_DB}")
        # Its own round trip: in one batch with CREATE EXTENSION the hook is
        # not loaded yet and `tv_post` becomes a plain table.
        _exec(_SRC_DB, target, "CREATE EXTENSION pg_tviews")
        _exec(
            _SRC_DB,
            target,
            _TREE,
            "INSERT INTO tb_user VALUES "
            "(1, '00000000-0000-0000-0000-000000000001', 'ada')",
            "INSERT INTO tb_post VALUES "
            "(1, '00000000-0000-0000-0000-0000000000a1', 1, 'first')",
        )
        subprocess.run(
            ["pg_dump", "-Fc", "-d", target.dsn(_SRC_DB), "-f", str(dump_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        _exec("postgres", target, f"CREATE DATABASE {_DST_DB}")

        result = restore_backup(
            backup_path=str(dump_path),
            db_name=_DST_DB,
            connection_url=target.dsn("postgres"),
            jobs=jobs,
        )
        assert result.success is True, result.error

        with psycopg.connect(target.dsn(_DST_DB), autocommit=True) as conn:
            restored = conn.execute(
                "SELECT data->>'author' FROM tv_post WHERE pk_post = 1"
            ).fetchone()
            assert restored == ("ada",)
            conn.execute("UPDATE tb_user SET name = 'grace' WHERE pk_user = 1")
            propagated = conn.execute(
                "SELECT data->>'author' FROM tv_post WHERE pk_post = 1"
            ).fetchone()
        assert propagated == ("grace",)
    finally:
        _drop_databases(target)

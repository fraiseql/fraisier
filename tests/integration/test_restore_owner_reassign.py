"""`restore.target_owner` reassigns ownership on a real server (#380).

`REASSIGN OWNED BY CURRENT_USER TO :"owner"` was passed to `psql -c`, which
hands its string to the server unlexed — psql never substitutes variables in
it. Measured with psql 15, 16 and 18 clients: all three answer
`syntax error at or near ":"`. The three tests that covered the call mocked
`_pg_cmd` and asserted the argv contained `-v`; they asserted the manifest
they built. This one executes.

The blast radius is deliberately bounded. The statement names `CURRENT_USER`,
and `CURRENT_USER` on a shared server is whoever ran the suite — a role that
may own every other database on the box, all of which `REASSIGN OWNED` would
hand to a throwaway role. So the session's role is set to a throwaway one with
``PGOPTIONS=-c role=…``: everything the reassignment can reach is something
this test created.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops import restore as restore_mod

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class _FakeRestorer:
    """confiture's restore, which is confiture's to test."""

    def restore(self, _options: object) -> MagicMock:
        return MagicMock(
            success=True,
            errors=[],
            matviews_deferred=None,
            matviews_refreshed=None,
            analyze_ran=False,
        )


@pytest.fixture
def owned_database(pg_target) -> Iterator[tuple[str, str, str, str]]:
    """A throwaway database, owned by a throwaway role, holding one table.

    Yields ``(dsn, db_name, source_role, target_role)``.
    """
    psycopg = pytest.importorskip("psycopg")

    suffix = uuid.uuid4().hex[:12]
    db_name = f"reassign_{suffix}"
    source = f"reassign_src_{suffix}"
    target = f"reassign_dst_{suffix}"

    admin = pg_target.dsn("postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"CREATE ROLE {source} NOLOGIN")
        conn.execute(f"CREATE ROLE {target} NOLOGIN")
        # REASSIGN requires the caller to hold privileges of the target role.
        conn.execute(f"GRANT {target} TO {source}")
        conn.execute(f"CREATE DATABASE {db_name} OWNER {source}")

    dsn = pg_target.dsn(db_name)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f"SET ROLE {source}")
        conn.execute("CREATE TABLE orders (id int)")

    try:
        yield dsn, db_name, source, target
    finally:
        # `options=""` overrides any PGOPTIONS the test set: the reassignment is
        # driven by pointing the session's role at the throwaway role, and that
        # role cannot drop roles.
        with psycopg.connect(admin, autocommit=True, options="") as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")
            for role in (target, source):
                conn.execute(f"DROP OWNED BY {role}")
                conn.execute(f"DROP ROLE IF EXISTS {role}")


def _table_owner(dsn: str) -> str:
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT tableowner FROM pg_tables WHERE tablename = 'orders'"
        ).fetchone()
    assert row is not None
    return row[0]


def test_restore_backup_reassigns_ownership_for_real(
    owned_database, tmp_path: Path, monkeypatch
):
    dsn, db_name, source, target = owned_database
    assert _table_owner(dsn) == source

    monkeypatch.setenv("PGOPTIONS", f"-c role={source}")
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"PGDMP")

    with patch.object(restore_mod, "DatabaseRestorer", _FakeRestorer):
        result = restore_mod.restore_backup(
            backup_path=str(dump),
            db_name=db_name,
            connection_url=dsn,
            db_owner=target,
        )

    assert result.success, result.error
    assert _table_owner(dsn) == target


def test_a_failed_reassignment_names_the_reassignment(
    owned_database, tmp_path: Path, monkeypatch
):
    """The error used to blame `pg_restore`, a step that had succeeded."""
    dsn, db_name, source, _target = owned_database
    monkeypatch.setenv("PGOPTIONS", f"-c role={source}")
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"PGDMP")

    with patch.object(restore_mod, "DatabaseRestorer", _FakeRestorer):
        result = restore_mod.restore_backup(
            backup_path=str(dump),
            db_name=db_name,
            connection_url=dsn,
            db_owner="no_such_role_here",
        )

    assert not result.success
    assert "reassign_owner failed" in result.error
    assert "pg_restore" not in result.error

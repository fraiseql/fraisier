"""The harness accepts exactly the servers the integration tests can use.

They create databases and roles, so the probe asks for ``CREATEDB`` and
``CREATEROLE`` (or a superuser) and nothing else. It also read
``unix_socket_directories``, which needs ``pg_read_all_settings``: a role
without it failed the probe, and an ``FRAISIER_TEST_PG_URL`` naming one fell
back to the local socket in silence, a "TCP" run that never used TCP.
"""

from __future__ import annotations

import uuid

import pytest

from .conftest import PgTarget, _probe


@pytest.fixture
def role_dsn(password_pg):
    """``make(attributes)``: the DSN of a new login role with *attributes*."""
    psycopg = pytest.importorskip("psycopg")
    made: list[str] = []

    def make(attributes: str) -> str:
        role = f"acme_probe_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(password_pg.dsn("postgres"), autocommit=True) as conn:
            conn.execute(f"CREATE ROLE {role} LOGIN {attributes} PASSWORD 'pw'")
        made.append(role)
        server = PgTarget(password_pg.host, password_pg.port, role, "pw")
        return server.dsn("postgres")

    yield make
    with psycopg.connect(password_pg.dsn("postgres"), autocommit=True) as conn:
        for role in made:
            conn.execute(f"DROP ROLE IF EXISTS {role}")


def test_createdb_and_createrole_without_superuser_probe_clean(role_dsn):
    assert _probe(role_dsn("CREATEDB CREATEROLE")) is not None


@pytest.mark.parametrize("attributes", ["CREATEDB", "CREATEROLE", ""])
def test_a_role_missing_either_is_not_usable(role_dsn, attributes):
    assert _probe(role_dsn(attributes)) is None

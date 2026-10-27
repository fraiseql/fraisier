"""Preflight works for an admin role that may create databases and nothing more.

That is all a preflight needs. Its schema restore requested
``-c lc_messages=C``, which only a superuser may set, so the server refused
every connection such a role made. CI's role is a superuser, so this
needs a server holding one that is not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest

from fraisier.dbops.preflight import PreflightDatabase
from fraisier.errors import DatabaseError

from .conftest import PW_PROBE_PASSWORD, PW_PROBE_ROLE

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def createdb_admin_url(password_pg) -> str:
    secret = quote(PW_PROBE_PASSWORD, safe="")
    hostport = f"{password_pg.host}:{password_pg.port}"
    return f"postgresql://{PW_PROBE_ROLE}:{secret}@{hostport}/postgres"


def test_a_clean_schema_restores(createdb_admin_url, tmp_path: Path):
    schema = tmp_path / "schema.sql"
    schema.write_text("CREATE TABLE orders (id int);\n")

    with PreflightDatabase(admin_url=createdb_admin_url) as pf:
        pf.restore_schema(schema)


def test_a_failed_statement_is_still_surfaced(createdb_admin_url, tmp_path: Path):
    schema = tmp_path / "schema.sql"
    schema.write_text(
        "CREATE TABLE orders (id int);\nCREATE TABLE broken (bad no_such_type);\n"
    )

    with (
        PreflightDatabase(admin_url=createdb_admin_url) as pf,
        pytest.raises(DatabaseError, match="no_such_type"),
    ):
        pf.restore_schema(schema)

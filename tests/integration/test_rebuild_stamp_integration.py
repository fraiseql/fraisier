"""Integration smoke test for ``_stamp_source_for_template`` against real PG.

Run with: ``FRAISIER_INTEGRATION=1 uv run pytest \\
    tests/integration/test_rebuild_stamp_integration.py``
"""

from __future__ import annotations

import logging
import os
import uuid

import psycopg
import pytest

from fraisier.dbops._url import replace_db_name
from fraisier.strategies import RebuildStrategy

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("FRAISIER_INTEGRATION") != "1",
        reason="set FRAISIER_INTEGRATION=1 to enable",
    ),
]


@pytest.fixture
def _stamp_test_db(pg_superuser_url):
    """Create a fresh DB with a seeded ``public.tb_version`` row."""
    db_name = f"stamptest_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_superuser_url, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {db_name}")  # ty: ignore[no-matching-overload]

    db_url = replace_db_name(pg_superuser_url, db_name)
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE public.tb_version (app_version text)")
        conn.execute("INSERT INTO public.tb_version (app_version) VALUES ('0.0.0')")

    yield db_url, db_name

    with psycopg.connect(pg_superuser_url, autocommit=True) as conn:
        conn.execute(  # ty: ignore[no-matching-overload]
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
        )
        conn.execute(  # ty: ignore[no-matching-overload]
            f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)"
        )


@pytest.fixture
def _empty_tb_version_db(pg_superuser_url):
    """Create a fresh DB with an empty ``public.tb_version`` (no INSERT)."""
    db_name = f"stamptest_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(pg_superuser_url, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {db_name}")  # ty: ignore[no-matching-overload]

    db_url = replace_db_name(pg_superuser_url, db_name)
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE public.tb_version (app_version text)")

    yield db_url, db_name

    with psycopg.connect(pg_superuser_url, autocommit=True) as conn:
        conn.execute(  # ty: ignore[no-matching-overload]
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
        )
        conn.execute(  # ty: ignore[no-matching-overload]
            f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)"
        )


def test_stamp_writes_value_into_tb_version(_stamp_test_db):
    """The UPDATE actually populates ``tb_version.app_version`` against a seeded row."""
    db_url, db_name = _stamp_test_db

    strategy = RebuildStrategy(app_version="9.9.9")
    strategy._stamp_source_for_template(db_name, connection_url=db_url)

    with psycopg.connect(db_url, autocommit=True) as conn:
        row = conn.execute("SELECT app_version FROM public.tb_version").fetchone()
    assert row is not None
    assert row[0] == "9.9.9"


def test_stamp_against_empty_tb_version_warns_and_inserts_nothing(
    _empty_tb_version_db, caplog
):
    """Empty ``tb_version`` triggers warn-and-skip; no row is created."""
    db_url, db_name = _empty_tb_version_db

    strategy = RebuildStrategy(app_version="9.9.9")
    with caplog.at_level(logging.WARNING, logger="fraisier.strategies._core"):
        strategy._stamp_source_for_template(db_name, connection_url=db_url)

    with psycopg.connect(db_url, autocommit=True) as conn:
        row = conn.execute("SELECT count(*) FROM public.tb_version").fetchone()
    assert row is not None
    assert row[0] == 0
    assert "tb_version" in caplog.text
    assert "empty" in caplog.text

"""Integration tests for fraisier.dbops.operations against real PostgreSQL.

Run with: uv run pytest tests/integration/test_operations_integration.py -m integration
"""

import uuid
from urllib.parse import urlparse

import psycopg
import pytest

pytestmark = pytest.mark.integration


class TestCreateDbIntegration:
    """Test create_db against a real PostgreSQL container."""

    def test_create_db_succeeds(self, pg_superuser_url):
        from fraisier.dbops.operations import create_db

        db_name = f"test_{uuid.uuid4().hex[:12]}"
        try:
            code, _stdout, stderr = create_db(db_name, connection_url=pg_superuser_url)
            assert code == 0, f"createdb failed: {stderr}"
        finally:
            with psycopg.connect(pg_superuser_url, autocommit=True) as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")

    def test_create_db_with_owner(self, pg_superuser_url):
        from fraisier.dbops.operations import create_db

        # Extract the actual superuser name from the container URL
        owner = urlparse(pg_superuser_url).username
        db_name = f"test_{uuid.uuid4().hex[:12]}"
        try:
            code, _, stderr = create_db(
                db_name, owner=owner, connection_url=pg_superuser_url
            )
            assert code == 0, f"createdb failed: {stderr}"

            # Verify owner
            with psycopg.connect(pg_superuser_url, autocommit=True) as conn:
                row = conn.execute(
                    "SELECT pg_catalog.pg_get_userbyid(datdba) "
                    "FROM pg_database WHERE datname = %s",
                    (db_name,),
                ).fetchone()
                assert row is not None
                assert row[0] == owner
        finally:
            with psycopg.connect(pg_superuser_url, autocommit=True) as conn:
                conn.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")


class TestDropDbIntegration:
    """Test drop_db against a real PostgreSQL container."""

    def test_drop_db_succeeds(self, pg_superuser_url):
        from fraisier.dbops.operations import create_db, drop_db

        db_name = f"test_{uuid.uuid4().hex[:12]}"
        create_db(db_name, connection_url=pg_superuser_url)

        code, _, stderr = drop_db(db_name, connection_url=pg_superuser_url)
        assert code == 0, f"dropdb failed: {stderr}"

        # Verify it's gone
        with psycopg.connect(pg_superuser_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s",
                (db_name,),
            ).fetchone()
            assert row[0] == 0

    def test_drop_nonexistent_db_returns_error(self, pg_superuser_url):
        from fraisier.dbops.operations import drop_db

        code, _, _ = drop_db("nonexistent_db_xyz", connection_url=pg_superuser_url)
        assert code != 0


class TestTerminateBackendsIntegration:
    """Test terminate_backends against a real PostgreSQL container."""

    def test_terminate_backends_succeeds(self, pg_test_db, pg_superuser_url):
        from fraisier.dbops.operations import terminate_backends

        _, db_name = pg_test_db
        code, _, stderr = terminate_backends(db_name, connection_url=pg_superuser_url)
        assert code == 0, f"terminate_backends failed: {stderr}"


class TestCheckDbExistsIntegration:
    """Test check_db_exists against a real PostgreSQL container."""

    def test_existing_db_returns_true(self, pg_test_db, pg_superuser_url):
        from fraisier.dbops.operations import check_db_exists

        _, db_name = pg_test_db
        assert check_db_exists(db_name, connection_url=pg_superuser_url) is True

    def test_nonexistent_db_returns_false(self, pg_superuser_url):
        from fraisier.dbops.operations import check_db_exists

        assert (
            check_db_exists("no_such_database_xyz", connection_url=pg_superuser_url)
            is False
        )


class TestRunPsqlIntegration:
    """Test run_psql and run_sql against a real PostgreSQL container."""

    def test_run_psql_executes_sql(self, pg_test_db, pg_superuser_url):
        from fraisier.dbops.operations import run_psql

        _, db_name = pg_test_db
        code, stdout, stderr = run_psql(
            "SELECT 1 AS result",
            db_name=db_name,
            connection_url=pg_superuser_url,
        )
        assert code == 0, f"psql failed: {stderr}"
        assert "1" in stdout

    def test_run_sql_returns_tuples_only(self, pg_test_db, pg_superuser_url):
        from fraisier.dbops.operations import run_sql

        _, db_name = pg_test_db
        code, stdout, stderr = run_sql(
            "SELECT 42",
            db_name=db_name,
            connection_url=pg_superuser_url,
        )
        assert code == 0, f"run_sql failed: {stderr}"
        assert stdout.strip() == "42"

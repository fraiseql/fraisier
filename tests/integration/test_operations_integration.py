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
                conn.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")  # ty: ignore[no-matching-overload]

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
                conn.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")  # ty: ignore[no-matching-overload]


def _wal_bytes_of(url: str, action) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        before = conn.execute("SELECT pg_current_wal_lsn()").fetchone()
        assert before is not None
        action()
        row = conn.execute(
            "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s)", (before[0],)
        ).fetchone()
        assert row is not None
        return int(row[0])


class TestCreateDbStrategyIntegration:
    """#479: what ``strategy="file_copy"`` does to a real server."""

    @pytest.fixture
    def source_db(self, pg_superuser_url):
        # ~40 MB, so the WAL a WAL_LOG copy writes stands well clear of the
        # noise other sessions on a shared server add.
        name = f"src_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(pg_superuser_url, autocommit=True) as conn:
            conn.execute(f"CREATE DATABASE {name}")  # ty: ignore[no-matching-overload]
        source_url = pg_superuser_url.rsplit("/", 1)[0] + f"/{name}"
        with psycopg.connect(source_url, autocommit=True) as conn:
            conn.execute(
                "CREATE TABLE t AS SELECT g, md5(g::text) || md5((-g)::text) AS pad "
                "FROM generate_series(1, 400000) g"
            )
        yield name
        _drop(pg_superuser_url, name)

    def test_file_copy_copies_the_template(self, pg_superuser_url, source_db):
        from fraisier.dbops.operations import create_db

        copy = f"Copy_{uuid.uuid4().hex[:8]}"
        try:
            code, _, stderr = create_db(
                copy,
                template=source_db,
                strategy="file_copy",
                connection_url=pg_superuser_url,
            )
            assert code == 0, stderr
            copy_url = pg_superuser_url.rsplit("/", 1)[0] + f"/{copy}"
            with psycopg.connect(copy_url) as conn:
                row = conn.execute("SELECT count(*) FROM t").fetchone()
            assert row == (400000,)
        finally:
            _drop(pg_superuser_url, f'"{copy}"')

    def test_file_copy_writes_almost_no_wal(self, pg_superuser_url, source_db):
        from fraisier.dbops.operations import create_db

        wal: dict[str | None, int] = {}
        for strategy in ("file_copy", None):
            copy = f"copy_{uuid.uuid4().hex[:8]}"
            try:
                wal[strategy] = _wal_bytes_of(
                    pg_superuser_url,
                    lambda copy=copy, strategy=strategy: create_db(
                        copy,
                        template=source_db,
                        strategy=strategy,
                        connection_url=pg_superuser_url,
                    ),
                )
            finally:
                _drop(pg_superuser_url, copy)
        # Measured on PG 17 at 3.7 GB: WAL_LOG 3,677 MB of WAL, FILE_COPY ~5 kB.
        assert wal[None] > 30 * 1024 * 1024
        assert wal["file_copy"] < 10 * 1024 * 1024

    def test_file_copy_refuses_a_source_with_other_sessions(
        self, pg_superuser_url, source_db
    ):
        from fraisier.dbops.operations import create_db

        source_url = pg_superuser_url.rsplit("/", 1)[0] + f"/{source_db}"
        copy = f"copy_{uuid.uuid4().hex[:8]}"
        # CREATE DATABASE waits ~5 s for other sessions to leave before failing.
        with psycopg.connect(source_url):
            code, _, stderr = create_db(
                copy,
                template=source_db,
                strategy="file_copy",
                connection_url=pg_superuser_url,
            )
        assert code != 0
        assert f'source database "{source_db}" is being accessed by other users' in (
            stderr
        )


def _drop(url: str, name: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")  # ty: ignore[no-matching-overload]


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
            assert row is not None
            assert row[0] == 0

    def test_drop_nonexistent_db_is_idempotent(self, pg_superuser_url):
        from fraisier.dbops.operations import drop_db

        # --if-exists makes dropping a nonexistent database a no-op (exit 0)
        code, _, _ = drop_db("nonexistent_db_xyz", connection_url=pg_superuser_url)
        assert code == 0


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

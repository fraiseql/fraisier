"""Integration tests for the full migration preflight flow.

Requires:
  - A local PostgreSQL instance accessible at FRAISIER_TEST_ADMIN_URL
    (default: postgresql://postgres@localhost/postgres)
  - fraiseql-confiture >= 0.9.4 with `confiture migrate preflight --against` support
  - pg_dump / pg_restore binaries

Run with:
    uv run pytest tests/test_preflight_e2e.py -v -m integration
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from fraisier.dbops.preflight import run_migration_preflight

# ---------------------------------------------------------------------------
# Constants and helpers
# ---------------------------------------------------------------------------

_DEFAULT_ADMIN_URL = "postgresql://postgres@localhost/postgres"


def _get_admin_url() -> str:
    import os

    return os.environ.get("FRAISIER_TEST_ADMIN_URL", _DEFAULT_ADMIN_URL)


def _pg_available(admin_url: str) -> bool:
    """Return True if PostgreSQL is reachable at admin_url."""
    try:
        result = subprocess.run(
            ["psql", admin_url, "-c", "SELECT 1", "-t", "-A"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run_psql(url: str, sql: str) -> str:
    result = subprocess.run(
        ["psql", url, "-c", sql, "-t", "-A"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _replace_db_in_url(admin_url: str, db_name: str) -> str:
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(admin_url)
    return urlunparse(parsed._replace(path=f"/{db_name}"))


def _count_preflight_dbs(admin_url: str) -> int:
    out = _run_psql(
        admin_url,
        "SELECT COUNT(*) FROM pg_database WHERE datname LIKE 'fraisier_preflight_%'",
    )
    return int(out.strip()) if out.strip().isdigit() else 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def admin_url():
    url = _get_admin_url()
    if not _pg_available(url):
        pytest.skip(f"PostgreSQL not reachable at {url}")  # ty: ignore[too-many-positional-arguments]
    return url


@pytest.fixture
def sample_backup(admin_url, tmp_path):
    """Create a source DB with tables, dump it, yield the dump path."""
    db_name = f"fraisier_test_source_{uuid.uuid4().hex[:8]}"
    _run_psql(admin_url, f"CREATE DATABASE {db_name}")
    source_url = _replace_db_in_url(admin_url, db_name)

    try:
        _run_psql(
            source_url,
            """
            CREATE TABLE users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE orders (
                id SERIAL PRIMARY KEY,
                user_id INT REFERENCES users(id),
                amount NUMERIC(10,2)
            );
            """,
        )

        dump_path = tmp_path / "test_backup.dump"
        subprocess.run(
            ["pg_dump", "-Fc", "-f", str(dump_path), source_url],
            check=True,
            capture_output=True,
        )
        yield dump_path
    finally:
        _run_psql(admin_url, f"DROP DATABASE IF EXISTS {db_name}")


@pytest.fixture
def confiture_config(tmp_path, admin_url):
    """Write a minimal confiture.yaml pointing at an initialized test database.

    The source DB is initialized with an empty confiture migration run so that
    ``tb_confiture`` exists and ``confiture migrate preflight --config`` can
    query it to determine which migrations are pending.
    """
    db_name = f"fraisier_test_cfg_{uuid.uuid4().hex[:8]}"
    _run_psql(admin_url, f"CREATE DATABASE {db_name}")
    cfg_url = _replace_db_in_url(admin_url, db_name)

    cfg = tmp_path / "confiture.yaml"
    cfg.write_text(f"database_url: {cfg_url}\n")

    # Initialize the confiture tracking table on the source DB so that
    # --config can query pending migrations (empty dir → 0 applied).
    empty_dir = tmp_path / "_init_migrations"
    empty_dir.mkdir()
    subprocess.run(
        [
            "confiture",
            "migrate",
            "up",
            "--config",
            str(cfg),
            "--migrations-dir",
            str(empty_dir),
        ],
        check=True,
        capture_output=True,
    )

    yield cfg

    _run_psql(admin_url, f"DROP DATABASE IF EXISTS {db_name}")


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPreflightE2EHappyPath:
    def test_all_migrations_pass(
        self, admin_url, sample_backup, confiture_config, tmp_path
    ):
        """Valid migrations pass when run against a schema-only copy."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "20260429120000_add_email.up.sql").write_text(
            "ALTER TABLE users ADD COLUMN email TEXT;\n"
        )
        (migrations_dir / "20260429120000_add_email.down.sql").write_text(
            "ALTER TABLE users DROP COLUMN IF EXISTS email;\n"
        )

        result = run_migration_preflight(
            backup_path=sample_backup,
            admin_url=admin_url,
            confiture_config=confiture_config,
            migrations_dir=migrations_dir,
        )

        assert result.all_passed is True
        assert result.schema_extraction_ms > 0
        assert result.total_ms > 0

    def test_empty_migrations_dir_returns_empty_result(
        self, admin_url, sample_backup, confiture_config, tmp_path
    ):
        """No pending migrations → empty result, all_passed True."""
        migrations_dir = tmp_path / "empty_migrations"
        migrations_dir.mkdir()

        result = run_migration_preflight(
            backup_path=sample_backup,
            admin_url=admin_url,
            confiture_config=confiture_config,
            migrations_dir=migrations_dir,
        )

        assert result.all_passed is True
        assert result.migrations == []


@pytest.mark.integration
class TestPreflightE2EInterdependent:
    def test_interdependent_pending_passes(
        self, admin_url, sample_backup, confiture_config, tmp_path
    ):
        """Two inter-dependent pending migrations preflight green (issue #250).

        ``V2`` creates a view over the table ``V1`` creates.  Because
        ``run_against`` applies pending migrations cumulatively (success →
        ``RELEASE SAVEPOINT``), ``V2`` sees ``V1``'s table and both pass.
        """
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "20260429130000_create_widgets.up.sql").write_text(
            "CREATE TABLE public.widgets (id BIGINT PRIMARY KEY);\n"
        )
        (migrations_dir / "20260429130000_create_widgets.down.sql").write_text(
            "DROP TABLE public.widgets;\n"
        )
        (migrations_dir / "20260429140000_add_widgets_view.up.sql").write_text(
            "CREATE OR REPLACE VIEW public.v_widgets AS "
            "SELECT id FROM public.widgets;\n"
        )
        (migrations_dir / "20260429140000_add_widgets_view.down.sql").write_text(
            "DROP VIEW public.v_widgets;\n"
        )

        result = run_migration_preflight(
            backup_path=sample_backup,
            admin_url=admin_url,
            confiture_config=confiture_config,
            migrations_dir=migrations_dir,
        )

        assert result.all_passed is True
        assert result.failure_count == 0
        versions = {m.version for m in result.migrations}
        assert versions == {"20260429130000", "20260429140000"}


@pytest.mark.integration
class TestPreflightE2EFailureDetection:
    def test_bad_migration_caught_before_restore(
        self, admin_url, sample_backup, confiture_config, tmp_path
    ):
        """Failing migration is detected and reported."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "20260429120000_bad_migration.up.sql").write_text(
            "ALTER TABLE nonexistent_table ADD COLUMN x TEXT;\n"
        )
        (migrations_dir / "20260429120000_bad_migration.down.sql").write_text(
            "ALTER TABLE nonexistent_table DROP COLUMN IF EXISTS x;\n"
        )

        result = run_migration_preflight(
            backup_path=sample_backup,
            admin_url=admin_url,
            confiture_config=confiture_config,
            migrations_dir=migrations_dir,
        )

        assert result.all_passed is False
        assert result.failure_count >= 1
        assert result.failures[0].error is not None

    def test_failure_includes_version(
        self, admin_url, sample_backup, confiture_config, tmp_path
    ):
        """Failed MigrationCheck carries the version string."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "20260429120001_bad.up.sql").write_text(
            "SELECT * FROM no_such_table;\n"
        )
        (migrations_dir / "20260429120001_bad.down.sql").write_text(
            "-- no-op rollback\n"
        )

        result = run_migration_preflight(
            backup_path=sample_backup,
            admin_url=admin_url,
            confiture_config=confiture_config,
            migrations_dir=migrations_dir,
        )

        assert result.failures[0].version is not None


@pytest.mark.integration
class TestPreflightE2ECleanup:
    def test_no_orphan_preflight_dbs_after_success(
        self, admin_url, sample_backup, confiture_config, tmp_path
    ):
        """Preflight DB is always dropped — no orphans after clean run."""
        before = _count_preflight_dbs(admin_url)
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()

        run_migration_preflight(
            backup_path=sample_backup,
            admin_url=admin_url,
            confiture_config=confiture_config,
            migrations_dir=migrations_dir,
        )

        after = _count_preflight_dbs(admin_url)
        assert after == before

    def test_no_orphan_preflight_dbs_after_failure(
        self, admin_url, sample_backup, confiture_config, tmp_path
    ):
        """Preflight DB is dropped even when migrations fail."""
        before = _count_preflight_dbs(admin_url)
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "20260429120000_bad.up.sql").write_text(
            "ALTER TABLE gone ADD COLUMN x TEXT;\n"
        )
        (migrations_dir / "20260429120000_bad.down.sql").write_text(
            "ALTER TABLE gone DROP COLUMN IF EXISTS x;\n"
        )

        run_migration_preflight(
            backup_path=sample_backup,
            admin_url=admin_url,
            confiture_config=confiture_config,
            migrations_dir=migrations_dir,
        )

        after = _count_preflight_dbs(admin_url)
        assert after == before

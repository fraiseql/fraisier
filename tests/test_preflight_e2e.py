"""Integration tests for the full migration preflight flow.

Requires:
  - A PostgreSQL instance whose role may CREATE/DROP databases, found by the
    shared harness in ``tests/integration/conftest.py`` (``FRAISIER_TEST_PG_URL``
    over TCP, or a local unix socket), or named outright by
    ``FRAISIER_TEST_ADMIN_URL``
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
from tests.integration.conftest import _discover_target, unavailable

# ---------------------------------------------------------------------------
# Constants and helpers
# ---------------------------------------------------------------------------

#: These tests ``CREATE``/``DROP`` a database per fixture, so they connect to
#: the maintenance database — never to the application database a
#: ``FRAISIER_TEST_PG_URL`` happens to name.
_MAINTENANCE_DB = "postgres"


def _get_admin_url() -> str | None:
    """The server these tests may create and drop databases on, or None.

    ``FRAISIER_TEST_ADMIN_URL`` wins when it is set. Otherwise the shared
    integration harness decides, which is what makes this module reach CI's
    postgres service over TCP from the ``FRAISIER_TEST_PG_URL`` every workflow
    already sets.

    This used to default to a passwordless ``postgres@localhost``. No workflow
    set the variable, the CI container runs as ``fraisier`` with a password, and
    so every one of the ten tests below skipped on every CI run — quietly —
    from the day they were written until #370.
    """
    import os

    explicit = os.environ.get("FRAISIER_TEST_ADMIN_URL")
    if explicit:
        return explicit
    target = _discover_target()
    return target.dsn(_MAINTENANCE_DB) if target is not None else None


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
    if url is None:
        unavailable("no PostgreSQL with createdb privilege is reachable")
    if not _pg_available(url):
        unavailable(f"PostgreSQL not reachable at {url}")
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


def _py_migration(
    version: str, class_name: str, name: str, up_sql: str, down_sql: str
) -> str:
    return (
        "from confiture.models.migration import Migration\n\n\n"
        f"class {class_name}(Migration):\n"
        f'    version = "{version}"\n'
        f'    name = "{name}"\n\n'
        "    def up(self):\n"
        f'        self.connection.execute("{up_sql}")\n\n'
        "    def down(self):\n"
        f'        self.connection.execute("{down_sql}")\n'
    )


@pytest.mark.integration
class TestPreflightBackupBehindTracking:
    """Issue #250 root cause: backup is behind the live tracking state.

    The predecessor (V0) is already applied in the backup, so it is *not*
    pending; the dependent feature migrations (V1, V2) are.  Before the fix,
    pending was resolved from the live config DB and V0 was wrongly re-run (or a
    later dependent ran without V0's object); the fix resolves pending from the
    backup-consistent preflight DB so only V1, V2 run — and pass.
    """

    def test_backup_behind_tracking_preflights_green(
        self, admin_url, confiture_config, tmp_path
    ):
        # Source DB with V0 (baseline) applied via confiture, then backed up.
        src = f"fraisier_test_src_{uuid.uuid4().hex[:8]}"
        _run_psql(admin_url, f"CREATE DATABASE {src}")
        src_url = _replace_db_in_url(admin_url, src)
        try:
            v0_dir = tmp_path / "v0"
            v0_dir.mkdir()
            (v0_dir / "20260101000000_baseline.py").write_text(
                _py_migration(
                    "20260101000000",
                    "Baseline",
                    "baseline",
                    "CREATE TABLE public.base (id BIGINT PRIMARY KEY)",
                    "DROP TABLE public.base",
                )
            )
            src_cfg = tmp_path / "src.yaml"
            src_cfg.write_text(f"database_url: {src_url}\n")
            subprocess.run(
                [
                    "confiture",
                    "migrate",
                    "up",
                    "--config",
                    str(src_cfg),
                    "--migrations-dir",
                    str(v0_dir),
                ],
                check=True,
                capture_output=True,
            )
            backup = tmp_path / "behind.dump"
            subprocess.run(
                ["pg_dump", "-Fc", "-f", str(backup), src_url],
                check=True,
                capture_output=True,
            )
        finally:
            _run_psql(admin_url, f"DROP DATABASE IF EXISTS {src}")

        # Full migration set: V0 (already in backup) + inter-dependent V1, V2.
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "20260101000000_baseline.py").write_text(
            _py_migration(
                "20260101000000",
                "Baseline",
                "baseline",
                "CREATE TABLE public.base (id BIGINT PRIMARY KEY)",
                "DROP TABLE public.base",
            )
        )
        (migrations_dir / "20260102000000_create_widgets.py").write_text(
            _py_migration(
                "20260102000000",
                "CreateWidgets",
                "create_widgets",
                "CREATE TABLE public.widgets (id BIGINT PRIMARY KEY)",
                "DROP TABLE public.widgets",
            )
        )
        (migrations_dir / "20260103000000_add_widgets_view.py").write_text(
            _py_migration(
                "20260103000000",
                "AddWidgetsView",
                "add_widgets_view",
                "CREATE OR REPLACE VIEW public.v_widgets AS SELECT id FROM public.widgets",
                "DROP VIEW public.v_widgets",
            )
        )

        result = run_migration_preflight(
            backup_path=backup,
            admin_url=admin_url,
            confiture_config=confiture_config,
            migrations_dir=migrations_dir,
        )

        assert result.all_passed is True, (
            "backup-behind-tracking should preflight green; got "
            f"{[(m.version, m.error) for m in result.failures]}"
        )
        # V0 is already applied in the backup → not pending; only V1, V2 run.
        # confiture 0.32 reports the count of checked migrations (it does not
        # enumerate passing ones): exactly 2 means V0 was excluded, not replayed.
        assert result.migrations_checked == 2


@pytest.mark.integration
class TestPreflightEmptyLedgerGuard:
    """Issue #262: a populated schema whose tracking ledger restored empty.

    When the backup's ``tb_confiture`` data fails to restore, the preflight DB
    has the full schema but an empty applied-migration ledger.  confiture would
    then treat the entire history as pending and replay every already-applied
    migration against the already-populated schema, failing en masse with false
    ``already exists`` errors.  The preflight must refuse with a precise error
    rather than emit that cascade — the already-applied migrations must NOT be
    replayed.
    """

    def test_empty_ledger_populated_schema_aborts(
        self, admin_url, confiture_config, tmp_path
    ):
        from fraisier.errors import DatabaseError

        # Source DB with V0 applied via confiture (creates `widgets` + a
        # tb_confiture row), then strip the ledger rows to mimic a backup whose
        # tracking data won't restore. The dump keeps the `widgets` table but an
        # empty tb_confiture — exactly the #262 shape.
        src = f"fraisier_test_emptyledger_{uuid.uuid4().hex[:8]}"
        _run_psql(admin_url, f"CREATE DATABASE {src}")
        src_url = _replace_db_in_url(admin_url, src)
        try:
            v0 = tmp_path / "v0"
            v0.mkdir()
            (v0 / "20260101000000_widgets.up.sql").write_text(
                "CREATE TABLE public.widgets (id BIGINT PRIMARY KEY);\n"
            )
            (v0 / "20260101000000_widgets.down.sql").write_text(
                "DROP TABLE public.widgets;\n"
            )
            src_cfg = tmp_path / "src.yaml"
            src_cfg.write_text(f"database_url: {src_url}\n")
            subprocess.run(
                [
                    "confiture",
                    "migrate",
                    "up",
                    "--config",
                    str(src_cfg),
                    "--migrations-dir",
                    str(v0),
                ],
                check=True,
                capture_output=True,
            )
            _run_psql(src_url, "DELETE FROM tb_confiture")  # ledger lost
            backup = tmp_path / "emptyledger.dump"
            subprocess.run(
                ["pg_dump", "-Fc", "-f", str(backup), src_url],
                check=True,
                capture_output=True,
            )
        finally:
            _run_psql(admin_url, f"DROP DATABASE IF EXISTS {src}")

        # Migration set still contains V0, whose object already exists in the
        # restored schema. With an empty ledger it would be wrongly replayed.
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "20260101000000_widgets.up.sql").write_text(
            "CREATE TABLE public.widgets (id BIGINT PRIMARY KEY);\n"
        )
        (migrations_dir / "20260101000000_widgets.down.sql").write_text(
            "DROP TABLE public.widgets;\n"
        )

        with pytest.raises(DatabaseError, match=r"262|0 rows|did not restore"):
            run_migration_preflight(
                backup_path=backup,
                admin_url=admin_url,
                confiture_config=confiture_config,
                migrations_dir=migrations_dir,
            )


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
        assert result.migrations_checked == 2

    def test_interdependent_python_pending_passes(
        self, admin_url, sample_backup, confiture_config, tmp_path
    ):
        """Same as above but with transactional *Python* migrations (#250 repro).

        The reporter's failing pair was two transactional Python migrations,
        V2 a view over V1's table — predecessor passed, dependent failed.
        """
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "20260429130000_create_widgets.py").write_text(
            "from confiture.models.migration import Migration\n\n\n"
            "class CreateWidgets(Migration):\n"
            '    version = "20260429130000"\n'
            '    name = "create_widgets"\n\n'
            "    def up(self):\n"
            '        self.connection.execute("CREATE TABLE public.widgets (id BIGINT PRIMARY KEY)")\n\n'
            "    def down(self):\n"
            '        self.connection.execute("DROP TABLE public.widgets")\n'
        )
        (migrations_dir / "20260429140000_add_widgets_view.py").write_text(
            "from confiture.models.migration import Migration\n\n\n"
            "class AddWidgetsView(Migration):\n"
            '    version = "20260429140000"\n'
            '    name = "add_widgets_view"\n\n'
            "    def up(self):\n"
            "        self.connection.execute(\n"
            '            "CREATE OR REPLACE VIEW public.v_widgets AS SELECT id FROM public.widgets"\n'
            "        )\n\n"
            "    def down(self):\n"
            '        self.connection.execute("DROP VIEW public.v_widgets")\n'
        )

        result = run_migration_preflight(
            backup_path=sample_backup,
            admin_url=admin_url,
            confiture_config=confiture_config,
            migrations_dir=migrations_dir,
        )

        assert result.all_passed is True, (
            "inter-dependent transactional Python migrations should preflight "
            f"green; got failures: {[(m.version, m.error) for m in result.failures]}"
        )
        assert result.failure_count == 0
        assert result.migrations_checked == 2


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

"""Integration tests for deployment strategies against real PostgreSQL.

Run with: uv run pytest tests/integration/test_strategies_integration.py -m integration
"""

import psycopg
import pytest

from fraisier.strategies import MigrateStrategy, RebuildStrategy

pytestmark = pytest.mark.integration


class TestMigrateStrategyIntegration:
    """MigrateStrategy against a real PostgreSQL database."""

    def test_execute_applies_pending_migrations(self, confiture_project):
        config_path, migrations_dir, test_url, _db_name = confiture_project

        strategy = MigrateStrategy()
        result = strategy.execute(
            config_path,
            migrations_dir=migrations_dir,
            database_url=test_url,
        )

        assert result.success
        assert result.migrations_applied >= 1

    def test_execute_is_idempotent(self, confiture_project):
        config_path, migrations_dir, test_url, _ = confiture_project

        strategy = MigrateStrategy()
        result1 = strategy.execute(
            config_path,
            migrations_dir=migrations_dir,
            database_url=test_url,
        )
        assert result1.success
        assert result1.migrations_applied >= 1

        # Second run — no pending migrations
        result2 = strategy.execute(
            config_path,
            migrations_dir=migrations_dir,
            database_url=test_url,
        )
        assert result2.success
        assert result2.migrations_applied == 0

    def test_rollback_reverses_migration(self, confiture_project):
        config_path, migrations_dir, test_url, _db_name = confiture_project

        strategy = MigrateStrategy()
        strategy.execute(
            config_path,
            migrations_dir=migrations_dir,
            database_url=test_url,
        )

        result = strategy.rollback(
            config_path,
            migrations_dir=migrations_dir,
            steps=1,
            database_url=test_url,
        )
        assert result.success
        assert result.migrations_applied == 1

        # Table should be gone after rollback
        with psycopg.connect(test_url) as conn:
            row = conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'tb_example'"
            ).fetchone()
            assert row[0] == 0


class TestRebuildStrategyIntegration:
    """RebuildStrategy against a real PostgreSQL database."""

    def test_execute_rebuilds_database(self, confiture_project, pg_superuser_url):
        config_path, migrations_dir, test_url, _db_name = confiture_project

        strategy = RebuildStrategy()
        result = strategy.execute(
            config_path,
            migrations_dir=migrations_dir,
            database_url=test_url,
        )

        assert result.success

        # Verify the table was created
        with psycopg.connect(test_url) as conn:
            row = conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'tb_example'"
            ).fetchone()
            assert row[0] == 1

    def test_rebuild_is_repeatable(self, confiture_project, pg_superuser_url):
        """Rebuild can be run multiple times (drop + recreate cycle)."""
        config_path, migrations_dir, test_url, _db_name = confiture_project

        strategy = RebuildStrategy()

        # First rebuild
        result1 = strategy.execute(
            config_path,
            migrations_dir=migrations_dir,
            database_url=test_url,
        )
        assert result1.success

        # Insert some data
        with psycopg.connect(test_url, autocommit=True) as conn:
            conn.execute("INSERT INTO tb_example (name) VALUES ('before_rebuild')")

        # Second rebuild — should drop and recreate
        result2 = strategy.execute(
            config_path,
            migrations_dir=migrations_dir,
            database_url=test_url,
        )
        assert result2.success

        # Data from before rebuild should be gone
        with psycopg.connect(test_url) as conn:
            row = conn.execute("SELECT count(*) FROM tb_example").fetchone()
            assert row[0] == 0

    def test_rollback_calls_execute(self, confiture_project, pg_superuser_url):
        """Rollback for rebuild is just another rebuild."""
        config_path, migrations_dir, test_url, _db_name = confiture_project

        strategy = RebuildStrategy()
        result = strategy.rollback(
            config_path,
            migrations_dir=migrations_dir,
            steps=1,
            database_url=test_url,
        )
        assert result.success

    def test_provisions_required_roles(self, confiture_project, pg_superuser_url):
        """Roles are created and granted to the db owner before schema apply."""
        config_path, migrations_dir, test_url, _db_name = confiture_project

        strategy = RebuildStrategy(required_roles=["test_core_role", "test_admin_role"])
        result = strategy.execute(
            config_path,
            migrations_dir=migrations_dir,
            database_url=test_url,
        )
        assert result.success

        # Verify the roles exist on the cluster
        with psycopg.connect(pg_superuser_url, autocommit=True) as conn:
            for role in ("test_core_role", "test_admin_role"):
                row = conn.execute(
                    "SELECT count(*) FROM pg_roles WHERE rolname = %s",
                    (role,),
                ).fetchone()
                assert row[0] == 1, f"Role {role} was not created"

            # Clean up roles after test
            for role in ("test_core_role", "test_admin_role"):
                conn.execute(f"DROP ROLE IF EXISTS {role}")

    def test_on_error_stop_aborts_on_bad_sql(
        self, pg_test_db, pg_superuser_url, tmp_path
    ):
        """ON_ERROR_STOP causes psql -f to fail fast on SQL errors."""
        import subprocess
        import textwrap

        test_url, _db_name = pg_test_db

        # Write a confiture project whose schema contains an error
        config_path = tmp_path / "confiture.yaml"
        config_path.write_text(
            textwrap.dedent(f"""\
            name: test
            database_url: "{test_url}"
            include_dirs:
              - "{tmp_path / "db" / "0_schema"}"
            """)
        )
        env_dir = tmp_path / "db" / "environments"
        env_dir.mkdir(parents=True)
        (env_dir / "test.yaml").write_text(
            textwrap.dedent(f"""\
            name: test
            database_url: "{test_url}"
            include_dirs:
              - "{tmp_path / "db" / "0_schema"}"
            """)
        )
        schema_dir = tmp_path / "db" / "0_schema" / "01_public"
        schema_dir.mkdir(parents=True)
        # Schema references a nonexistent role → will error
        (schema_dir / "010_bad.sql").write_text(
            "CREATE SCHEMA bad_schema AUTHORIZATION nonexistent_role;\n"
        )
        migrations_dir = tmp_path / "db" / "migrations"
        migrations_dir.mkdir(parents=True)
        (migrations_dir / "001_initial.up.sql").write_text("SELECT 1;\n")
        (migrations_dir / "001_initial.down.sql").write_text("SELECT 1;\n")

        strategy = RebuildStrategy()
        with pytest.raises(subprocess.CalledProcessError):
            strategy.execute(
                config_path,
                migrations_dir=migrations_dir,
                database_url=test_url,
            )

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

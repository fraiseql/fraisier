"""Integration tests for confiture migration operations.

These tests use real confiture against a temporary PostgreSQL database
(via testcontainers or a local instance). They verify that fraisier's
thin wrapper over confiture behaves correctly with real SQL.

Run with: uv run pytest tests/integration/ -m integration
"""

import os
import textwrap
from pathlib import Path

import psycopg
import pytest

# Skip if no PostgreSQL available
_PG_URL = os.getenv("FRAISIER_TEST_PG_URL")
pytestmark = pytest.mark.integration


def _write_confiture_config(tmp_path: Path, db_url: str) -> Path:
    """Write a minimal confiture.yaml for testing."""
    config_path = tmp_path / "confiture.yaml"
    config_path.write_text(
        textwrap.dedent(f"""\
        name: test
        database_url: "{db_url}"
        include_dirs:
          - "{tmp_path / "migrations"}"
        """)
    )
    return config_path


def _write_migration(migrations_dir: Path, version: str, up_sql: str, down_sql: str):
    """Write a migration pair (up + down)."""
    migrations_dir.mkdir(parents=True, exist_ok=True)
    (migrations_dir / f"{version}_test.up.sql").write_text(up_sql)
    (migrations_dir / f"{version}_test.down.sql").write_text(down_sql)


@pytest.fixture
def pg_url():
    """Get PostgreSQL URL, skip if not available."""
    url = os.getenv("FRAISIER_TEST_PG_URL")
    if not url:
        pytest.skip("FRAISIER_TEST_PG_URL not set — skipping integration test")
    return url


@pytest.fixture
def migration_env(tmp_path, pg_url):
    """Set up a clean migration environment with confiture config and migration dir.

    Drops all user tables and confiture's tracking table before each test
    to ensure complete isolation.
    """
    with psycopg.connect(pg_url) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Drop all user tables and the tracking table
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            tables = [row[0] for row in cur.fetchall()]
            for table in tables:
                cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')

    migrations_dir = tmp_path / "migrations"
    config_path = _write_confiture_config(tmp_path, pg_url)
    return config_path, migrations_dir


class TestConfitureIntegration:
    """Test confiture Python API with real database."""

    def test_migrate_up_applies_migrations(self, migration_env):
        from fraisier.dbops.confiture import migrate_up

        config_path, migrations_dir = migration_env
        _write_migration(
            migrations_dir,
            "001",
            "CREATE TABLE test_orders (id SERIAL PRIMARY KEY, name TEXT);",
            "DROP TABLE test_orders;",
        )

        result = migrate_up(config_path, migrations_dir=migrations_dir)
        assert result.success
        assert result.steps_applied == 1

    def test_migrate_down_reverses_migrations(self, migration_env):
        from fraisier.dbops.confiture import migrate_down, migrate_up

        config_path, migrations_dir = migration_env
        _write_migration(
            migrations_dir,
            "001",
            "CREATE TABLE test_rollback (id SERIAL PRIMARY KEY);",
            "DROP TABLE test_rollback;",
        )

        migrate_up(config_path, migrations_dir=migrations_dir)
        result = migrate_down(config_path, migrations_dir=migrations_dir, steps=1)
        assert result.success
        assert result.steps_applied == 1

    def test_migrate_up_is_idempotent(self, migration_env):
        from fraisier.dbops.confiture import migrate_up

        config_path, migrations_dir = migration_env
        _write_migration(
            migrations_dir,
            "001",
            "CREATE TABLE test_idempotent (id SERIAL PRIMARY KEY);",
            "DROP TABLE test_idempotent;",
        )

        result1 = migrate_up(config_path, migrations_dir=migrations_dir)
        assert result1.steps_applied == 1

        # Second run should be a no-op
        result2 = migrate_up(config_path, migrations_dir=migrations_dir)
        assert result2.success
        assert result2.steps_applied == 0

    def test_preflight_detects_irreversible(self, migration_env):
        from fraisier.dbops.confiture import (
            IrreversibleMigrationError,
            preflight,
        )

        config_path, migrations_dir = migration_env
        migrations_dir.mkdir(parents=True, exist_ok=True)
        # Write only an up file, no down
        (migrations_dir / "001_irreversible.up.sql").write_text(
            "CREATE TABLE no_rollback (id SERIAL PRIMARY KEY);"
        )

        with pytest.raises(IrreversibleMigrationError):
            preflight(
                config_path,
                migrations_dir=migrations_dir,
                allow_irreversible=False,
            )


class TestScaffoldArtifactValidation:
    """Validate that scaffold generates syntactically correct artifacts."""

    def test_no_unexpanded_template_variables(self, tmp_path):
        """Scaffold output must not contain unexpanded {{ }} or {variable}."""
        from fraisier.config import FraisierConfig
        from fraisier.scaffold.renderer import ScaffoldRenderer

        config_yaml = tmp_path / "fraises.yaml"
        scaffold_dir = tmp_path / "scaffold"
        config_yaml.write_text(
            f"""\
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/myapi
        systemd_service: myapi.service
        health_check:
          url: http://localhost:8000/health
scaffold:
  output_dir: "{scaffold_dir}"
  deploy_user: fraisier
"""
        )
        config = FraisierConfig(str(config_yaml))
        renderer = ScaffoldRenderer(config)
        rendered = renderer.render(dry_run=False)

        for rel_path in rendered:
            full_path = scaffold_dir / rel_path
            if not full_path.exists():
                continue
            # GitHub Actions .yml files legitimately use ${{ }}
            if full_path.suffix in (".yml", ".yaml"):
                continue
            content = full_path.read_text()
            assert "{{" not in content, f"Unexpanded template in {rel_path}"
            assert "}}" not in content, f"Unexpanded template in {rel_path}"

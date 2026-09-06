"""Integration tests for confiture migration operations.

These run real confiture against a database of their own, created on whatever
server ``pg_target`` discovered and dropped afterwards.

**Each test gets a fresh database, and this module drops nothing else.** It used
to read ``FRAISIER_TEST_PG_URL`` itself and begin every test by dropping every
table in ``public`` of whatever that named — ``CASCADE``, ``autocommit=True``,
with no check that the target was a throwaway. Pointed at a real database, that
emptied it (#386). Isolation now comes from owning the database, not from
clearing someone else's.

Run with: uv run pytest tests/integration/ -m integration
"""

from __future__ import annotations

import textwrap
import uuid
from typing import TYPE_CHECKING

import psycopg
import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

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
def pg_url(pg_target) -> Iterator[str]:
    """A database of this test's own, dropped when it finishes.

    A fresh database is empty, which is the isolation these tests wanted, and
    it bounds what the teardown can destroy to something this fixture created.
    """
    db_name = f"confiture_it_{uuid.uuid4().hex[:12]}"
    admin = pg_target.dsn("postgres")

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')  # ty: ignore[no-matching-overload]

    try:
        yield pg_target.dsn(db_name)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')  # ty: ignore[no-matching-overload]


@pytest.fixture
def migration_env(tmp_path, pg_url):
    """A confiture config and migrations dir pointed at this test's database."""
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

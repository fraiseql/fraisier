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

        with pytest.raises(IrreversibleMigrationError) as exc:
            preflight(
                config_path,
                migrations_dir=migrations_dir,
                allow_irreversible=False,
            )

        assert "001" in str(exc.value)

    def test_preflight_names_the_setting_that_lets_it_through(self, migration_env):
        """#487: the refusal named `--no-rollback`, which turns off the
        health-check rollback and leaves this refusal in place."""
        from fraisier.dbops.confiture import (
            IrreversibleMigrationError,
            preflight,
        )

        config_path, migrations_dir = migration_env
        migrations_dir.mkdir(parents=True, exist_ok=True)
        (migrations_dir / "001_irreversible.up.sql").write_text(
            "CREATE TABLE no_rollback (id SERIAL PRIMARY KEY);"
        )

        with pytest.raises(IrreversibleMigrationError) as exc:
            preflight(config_path, migrations_dir=migrations_dir)

        assert "`allow_irreversible: true`" in str(exc.value)
        assert "--no-rollback" not in str(exc.value)

    def test_preflight_passes_a_reversible_python_migration(self, migration_env):
        from fraisier.dbops.confiture import preflight

        config_path, migrations_dir = migration_env
        migrations_dir.mkdir(parents=True, exist_ok=True)
        (migrations_dir / "001_add_r.py").write_text(_REVERSIBLE_PY)

        preflight(config_path, migrations_dir=migrations_dir)

    def test_preflight_ignores_an_applied_irreversible_migration(self, migration_env):
        """Only what this deploy would apply can block it."""
        from fraisier.dbops.confiture import migrate_up, preflight

        config_path, migrations_dir = migration_env
        migrations_dir.mkdir(parents=True, exist_ok=True)
        (migrations_dir / "001_add_u.py").write_text(_EMPTY_DOWN_PY)
        migrate_up(config_path, migrations_dir=migrations_dir)
        _write_migration(
            migrations_dir,
            "002",
            "CREATE TABLE later (id int);",
            "DROP TABLE later;",
        )

        preflight(config_path, migrations_dir=migrations_dir)


_EMPTY_DOWN_PY = """from confiture.models.migration import Migration


class AddU(Migration):
    version = "001"
    name = "add_u"

    def up(self) -> None:
        self.execute("CREATE TABLE empty_down_u (id int)")

    def down(self) -> None:
        pass
"""


_REVERSIBLE_PY = """from confiture.models.migration import Migration


class AddR(Migration):
    version = "001"
    name = "add_r"

    def up(self) -> None:
        self.execute("CREATE TABLE reversible_r (id int)")

    def down(self) -> None:
        self.execute("DROP TABLE reversible_r")
"""


class TestEmptyPythonDown:
    """#484: confiture 1.40 counts a ``.py`` migration whose ``down()`` does
    nothing as irreversible. A ``migrate`` deploy without
    ``allow_irreversible`` passes ``require_reversible`` and is now refused
    before anything runs; before 1.40 it applied."""

    def _write(self, migrations_dir):
        migrations_dir.mkdir(parents=True, exist_ok=True)
        (migrations_dir / "001_add_u.py").write_text(_EMPTY_DOWN_PY)

    def test_fraisiers_own_preflight_refuses_it(self, migration_env):
        """#487: preflight reads confiture's verdict, so it no longer misses a
        ``.py`` migration by scanning for ``.down.sql`` files alone."""
        from fraisier.dbops.confiture import IrreversibleMigrationError, preflight

        config_path, migrations_dir = migration_env
        self._write(migrations_dir)

        with pytest.raises(IrreversibleMigrationError) as exc:
            preflight(
                config_path, migrations_dir=migrations_dir, allow_irreversible=False
            )

        assert "001" in str(exc.value)
        assert "`allow_irreversible: true`" in str(exc.value)

    def test_preflight_lets_it_through_with_allow_irreversible(self, migration_env):
        from fraisier.dbops.confiture import preflight

        config_path, migrations_dir = migration_env
        self._write(migrations_dir)

        preflight(config_path, migrations_dir=migrations_dir, allow_irreversible=True)

    def test_require_reversible_refuses_it_and_applies_nothing(self, migration_env):
        from fraisier.dbops.confiture import has_pending, migrate_up
        from fraisier.errors import MigrationError

        config_path, migrations_dir = migration_env
        self._write(migrations_dir)

        with pytest.raises(
            MigrationError, match="a down\\(\\) that does nothing"
        ) as exc:
            migrate_up(
                config_path, migrations_dir=migrations_dir, require_reversible=True
            )

        assert exc.value.steps_applied == 0
        assert has_pending(config_path, migrations_dir=migrations_dir)

    def test_the_refusal_names_fraisiers_setting(self, migration_env):
        """#487: confiture's text names its own library argument,
        ``require_reversible``, which an operator cannot set."""
        from fraisier.dbops.confiture import migrate_up
        from fraisier.errors import MigrationError

        config_path, migrations_dir = migration_env
        self._write(migrations_dir)

        with pytest.raises(MigrationError) as exc:
            migrate_up(
                config_path, migrations_dir=migrations_dir, require_reversible=True
            )

        assert "`allow_irreversible: true`" in str(exc.value)
        assert "require_reversible" not in str(exc.value)

    def test_allow_irreversible_applies_it(self, migration_env):
        from fraisier.dbops.confiture import migrate_up

        config_path, migrations_dir = migration_env
        self._write(migrations_dir)

        result = migrate_up(
            config_path, migrations_dir=migrations_dir, require_reversible=False
        )

        assert result.steps_applied == 1


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

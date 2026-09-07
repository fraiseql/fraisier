"""Deploying a migration that loses data (#398).

Confiture 1.2.0 added a **destructive gate**: a migration that loses data — a
dropped table or column, a narrowed type — is generated under
``migration.destructive: gated`` (the default) carrying a
``-- confiture:destructive`` directive, or ``destructive = True`` on a Python
migration, and ``migrate up`` refuses it with ``VALID_002`` (exit 5) unless it
runs with ``allow_destructive``.

fraisier never passed that flag, so such a migration could not be deployed at
all — and the refusal arrived as a raw ``confiture.exceptions.ValidationError``,
which is not a fraisier ``MigrationError``, so ``_run_strategy``'s handler did
not catch it and the operator got an upstream traceback pointing at a CLI flag
fraisier does not run.

These tests use a real database and the real confiture because the gate lives
upstream: a mocked ``Migrator`` would accept ``allow_destructive`` and prove
nothing about whether confiture acted on it. ``test_allowed_destructive_migration_applies``
asserts the **effect** — the table exists afterwards — for the same reason.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from fraisier.dbops.confiture import migrate_up
from fraisier.errors import MigrationError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

psycopg = pytest.importorskip("psycopg")

_DB = "fraisier_it_destructive"

_DESTRUCTIVE = """from confiture.models.migration import Migration


class DropLegacy(Migration):
    version = "20260908000000"
    name = "drop_legacy"
    destructive = True

    def up(self):
        self.connection.execute("CREATE TABLE public.tb_kept (id BIGINT PRIMARY KEY)")

    def down(self):
        self.connection.execute("DROP TABLE public.tb_kept")
"""

_ORDINARY = _DESTRUCTIVE.replace("    destructive = True\n", "")


def _exec(db: str, target, *statements: str) -> None:
    with psycopg.connect(target.dsn(db), autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


@pytest.fixture
def destructive_db(pg_target):
    with contextlib.suppress(Exception):
        _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")
    _exec("postgres", pg_target, f"CREATE DATABASE {_DB}")
    try:
        yield pg_target
    finally:
        with contextlib.suppress(Exception):
            _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")


def _project(tmp_path: Path, destructive_db, migration: str) -> Path:
    root = tmp_path / "app"
    (root / "db" / "migrations").mkdir(parents=True)
    (root / "db" / "environments").mkdir(parents=True)
    (root / "db" / "migrations" / "20260908000000_drop_legacy.py").write_text(migration)
    (root / "db" / "environments" / "production.yaml").write_text(
        f"name: production\ndatabase_url: {destructive_db.dsn(_DB)}\ninclude_dirs: []\n"
    )
    return root


def _migrate(project: Path, **kwargs):
    return migrate_up(
        project / "db" / "environments" / "production.yaml",
        migrations_dir=project / "db" / "migrations",
        project_dir=project,
        **kwargs,
    )


def _table_exists(destructive_db) -> bool:
    with psycopg.connect(destructive_db.dsn(_DB)) as conn:
        row = conn.execute(
            "SELECT to_regclass('public.tb_kept') IS NOT NULL"
        ).fetchone()
        return bool(row and row[0])


class TestRefusedByDefault:
    def test_it_is_a_fraisier_error_not_a_raw_confiture_one(
        self, tmp_path, destructive_db
    ) -> None:
        """`_run_strategy` catches `MigrationError`; a raw one sails past it."""
        project = _project(tmp_path, destructive_db, _DESTRUCTIVE)

        with pytest.raises(MigrationError) as excinfo:
            _migrate(project)

        assert excinfo.value.steps_applied == 0
        assert not _table_exists(destructive_db)

    def test_the_message_names_the_fraisier_knob(
        self, tmp_path, destructive_db
    ) -> None:
        """Quoting `migrate up --allow-destructive` sends the operator nowhere.

        fraisier drives confiture in-process and never runs that CLI, so the
        remediation has to be the config key they can actually set.
        """
        project = _project(tmp_path, destructive_db, _DESTRUCTIVE)

        with pytest.raises(MigrationError) as excinfo:
            _migrate(project)

        message = str(excinfo.value)
        assert "allow_destructive" in message
        assert "20260908000000_drop_legacy.py" in message


class TestAllowed:
    def test_allowed_destructive_migration_applies(
        self, tmp_path, destructive_db
    ) -> None:
        """The effect, not the call: a flag accepted and dropped fails here."""
        project = _project(tmp_path, destructive_db, _DESTRUCTIVE)

        result = _migrate(project, allow_destructive=True)

        assert result.success is True
        assert result.steps_applied == 1
        assert _table_exists(destructive_db)


class TestTheControl:
    @pytest.mark.parametrize("allow", [False, True])
    def test_an_ordinary_migration_is_unaffected(
        self, tmp_path, destructive_db, allow: bool
    ) -> None:
        """Otherwise the tests above might be measuring migrations in general."""
        project = _project(tmp_path, destructive_db, _ORDINARY)

        result = _migrate(project, allow_destructive=allow)

        assert result.success is True
        assert result.steps_applied == 1
        assert _table_exists(destructive_db)

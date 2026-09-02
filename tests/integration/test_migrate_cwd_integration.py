"""A real migration resolving a project-relative path, from a foreign cwd (#371).

The scaffolded shell path runs ``cd "${PROJECT_DIR}" && confiture migrate``. The
in-process path chdir'd in one caller only, so a migration's ``up()`` resolved
``db/schema/fn.sql`` against the app while the ``down()`` rolling back that same
deploy resolved it against ``/home/<deploy_user>``.

Nothing pinned that, because fraisier's own tree contains no migration at all.
These tests supply one and run it from a directory that is deliberately not the
project.

The migration reads its SQL with ``Path(...).read_text()`` rather than
``Migration.execute_file``. That is the whole point: confiture 0.46 resolves
``execute_file`` against the migration's own project root, so a test written on
it would pass with or without fraisier holding the invariant, and would keep
passing after a regression. ``read_text`` is resolved by nobody but the cwd.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from confiture.exceptions import MigrationError as ConfitureMigrationError

from fraisier.dbops.confiture import migrate_down, migrate_up
from fraisier.errors import MigrationError

pytestmark = pytest.mark.integration

psycopg = pytest.importorskip("psycopg")

_DB = "fraisier_it_migrate_cwd"

_MIGRATION = """from pathlib import Path

from confiture.models.migration import Migration


class CreateWidgets(Migration):
    version = "20260903000000"
    name = "create_widgets"

    def up(self):
        self.connection.execute(Path("db/schema/widgets.sql").read_text())

    def down(self):
        self.connection.execute(Path("db/schema/widgets_down.sql").read_text())
"""


def _exec(db, target, *statements):
    with psycopg.connect(target.dsn(db), autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


def _has_widgets(target) -> bool:
    with psycopg.connect(target.dsn(_DB), autocommit=True) as conn:
        return conn.execute(
            "SELECT to_regclass('public.widgets') IS NOT NULL"
        ).fetchone()[0]


@pytest.fixture
def migrate_db(pg_target):
    with contextlib.suppress(Exception):
        _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")
    _exec("postgres", pg_target, f"CREATE DATABASE {_DB}")
    try:
        yield pg_target
    finally:
        with contextlib.suppress(Exception):
            _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")


@pytest.fixture
def project(tmp_path, migrate_db):
    """A project whose migration reads SQL from a path relative to the project."""
    root = tmp_path / "app"
    (root / "db" / "migrations").mkdir(parents=True)
    (root / "db" / "schema").mkdir(parents=True)

    (root / "db" / "schema" / "widgets.sql").write_text(
        "CREATE TABLE public.widgets (id BIGINT PRIMARY KEY);\n"
    )
    (root / "db" / "schema" / "widgets_down.sql").write_text(
        "DROP TABLE public.widgets;\n"
    )
    (root / "db" / "migrations" / "20260903000000_create_widgets.py").write_text(
        _MIGRATION
    )
    (root / "confiture.yaml").write_text(
        f"database_url: {migrate_db.dsn(_DB)}\nname: app\ninclude_dirs: []\n"
    )
    return root


@pytest.fixture
def foreign_cwd(tmp_path, monkeypatch):
    """Stand somewhere that is emphatically not the project.

    On the deploy worker this is ``/home/<deploy_user>``, which
    ``deploy-service.j2`` sets as ``WorkingDirectory`` — not a decision anyone
    made about migrations, just where the unit happens to start.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return elsewhere


class TestMigrateUpFromAForeignCwd:
    def test_the_migration_finds_its_file_when_the_project_is_named(
        self, project, foreign_cwd, migrate_db
    ):
        result = migrate_up(
            project / "confiture.yaml",
            migrations_dir=project / "db" / "migrations",
            project_dir=project,
        )

        assert result.success is True
        assert result.steps_applied == 1
        assert _has_widgets(migrate_db) is True

    def test_without_the_project_the_same_migration_cannot_find_it(
        self, project, foreign_cwd, migrate_db
    ):
        """The negative control: this is the failure #371 reported.

        If this ever stops raising, the chdir is no longer what makes the
        positive case above pass, and the test has stopped testing anything.
        """
        with pytest.raises(MigrationError) as excinfo:
            migrate_up(
                project / "confiture.yaml",
                migrations_dir=project / "db" / "migrations",
            )

        assert "widgets.sql" in str(excinfo.value)
        assert _has_widgets(migrate_db) is False

    def test_the_working_directory_is_given_back(
        self, project, foreign_cwd, migrate_db
    ):
        migrate_up(
            project / "confiture.yaml",
            migrations_dir=project / "db" / "migrations",
            project_dir=project,
        )

        assert Path.cwd().resolve() == foreign_cwd.resolve()

    def test_the_working_directory_is_given_back_after_a_failure(
        self, project, foreign_cwd, migrate_db
    ):
        """A failed migrate is followed by a rollback, which must start clean."""
        (project / "db" / "schema" / "widgets.sql").write_text(
            "CREATE TABLE public.widgets (id BIGINT PRIMARY KEY REFERENCES nope);\n"
        )

        with pytest.raises(MigrationError):
            migrate_up(
                project / "confiture.yaml",
                migrations_dir=project / "db" / "migrations",
                project_dir=project,
            )

        assert Path.cwd().resolve() == foreign_cwd.resolve()


class TestMigrateDownFromAForeignCwd:
    """The half that was broken even after #10 fixed the forward path.

    ``APIDeployer._run_strategy`` has chdir'd since #10, so ``up()`` ran in the
    app. ``_rollback_database`` never did — so the rollback of a failed deploy
    resolved the same relative path somewhere else entirely.
    """

    def test_the_rollback_finds_its_file_when_the_project_is_named(
        self, project, foreign_cwd, migrate_db
    ):
        migrate_up(
            project / "confiture.yaml",
            migrations_dir=project / "db" / "migrations",
            project_dir=project,
        )
        assert _has_widgets(migrate_db) is True

        result = migrate_down(
            project / "confiture.yaml",
            migrations_dir=project / "db" / "migrations",
            steps=1,
            project_dir=project,
        )

        assert result.success is True
        assert _has_widgets(migrate_db) is False

    def test_without_the_project_the_rollback_cannot_find_it(
        self, project, foreign_cwd, migrate_db
    ):
        """The negative control for the down direction.

        Note the channel: confiture raises out of ``m.down()`` rather than
        returning a failed result, so this escapes ``migrate_down`` as an
        exception despite its "best-effort — logs errors" contract. The
        ``result.success is False`` path only covers the result confiture
        returns, not the one it throws.
        """
        migrate_up(
            project / "confiture.yaml",
            migrations_dir=project / "db" / "migrations",
            project_dir=project,
        )

        with pytest.raises(ConfitureMigrationError, match=r"widgets_down\.sql"):
            migrate_down(
                project / "confiture.yaml",
                migrations_dir=project / "db" / "migrations",
                steps=1,
            )

        assert _has_widgets(migrate_db) is True

    def test_the_working_directory_is_given_back(
        self, project, foreign_cwd, migrate_db
    ):
        migrate_up(
            project / "confiture.yaml",
            migrations_dir=project / "db" / "migrations",
            project_dir=project,
        )

        migrate_down(
            project / "confiture.yaml",
            migrations_dir=project / "db" / "migrations",
            steps=1,
            project_dir=project,
        )

        assert Path.cwd().resolve() == foreign_cwd.resolve()

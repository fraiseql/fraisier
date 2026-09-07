"""The drift gate against a real PostgreSQL and a real confiture (#395).

``tests/test_drift_gate.py`` asserts the argv. This module executes it — the
house rule that an asserted command line is worth nothing until something runs
it.

The failure being reproduced is the one the issue was filed about, and the
language matters. ``CREATE OR REPLACE FUNCTION`` with a **PL/pgSQL** body stores
that body without resolving what it references, so a function reading a column
the migration never added is created happily, the migration exits 0, and the
deploy reports success; the error appears at the function's next call. A
``LANGUAGE sql`` body *is* validated at creation time and fails the migration
immediately — a reproduction written in SQL would prove nothing, so
``test_the_migration_itself_reports_success`` pins that the migration really is
silent before the gate is credited with catching anything.

The DDL is schema-qualified (``core.tb_widget``) on purpose. Against confiture
0.46 this check reported ``CRITICAL MISSING_TABLE core`` on a database applied
verbatim from its own DDL — ``drift.py`` matched the table name with ``(\\w+)``,
which does not match a dot, and the live side was read with a hardcoded
``table_schema = 'public'``. Both were fixed in 1.0.0, and
``test_a_matching_multi_schema_database_reports_no_drift`` is what holds the
floor in ``pyproject.toml`` honest.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from fraisier.dbops.confiture import migrate_up
from fraisier.dbops.drift import check_schema_drift

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

psycopg = pytest.importorskip("psycopg")

_DB = "fraisier_it_post_migrate_check"

#: A migration that creates the table **without** ``label`` and then a PL/pgSQL
#: function whose body reads it. Both statements succeed; the schema is now a
#: column short of what the DDL declares, and nothing has said so.
_MIGRATION_MISSING_COLUMN = '''from confiture.models.migration import Migration


class CreateWidget(Migration):
    version = "20260907000000"
    name = "create_widget"

    def up(self):
        self.connection.execute(
            "CREATE SCHEMA IF NOT EXISTS core"
        )
        self.connection.execute(
            "CREATE TABLE core.tb_widget (id BIGINT PRIMARY KEY, serial TEXT NOT NULL)"
        )
        self.connection.execute(
            """CREATE OR REPLACE FUNCTION core.fn_widget_label(p_id BIGINT)
            RETURNS TEXT LANGUAGE plpgsql AS $$
            BEGIN
                RETURN (SELECT label FROM core.tb_widget WHERE id = p_id);
            END $$"""
        )

    def down(self):
        self.connection.execute("DROP SCHEMA core CASCADE")
'''

#: The same migration, with the column the DDL declares.
_MIGRATION_COMPLETE = _MIGRATION_MISSING_COLUMN.replace(
    "id BIGINT PRIMARY KEY, serial TEXT NOT NULL",
    "id BIGINT PRIMARY KEY, serial TEXT NOT NULL, label TEXT",
)

#: What the checkout says the schema should be — schema-qualified, and with the
#: column the function's body reads.
_DDL = """CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE core.tb_widget (
    id BIGINT PRIMARY KEY,
    serial TEXT NOT NULL,
    label TEXT
);
"""


def _exec(db: str, target, *statements: str) -> None:
    with psycopg.connect(target.dsn(db), autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


@pytest.fixture
def drift_db(pg_target):
    with contextlib.suppress(Exception):
        _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")
    _exec("postgres", pg_target, f"CREATE DATABASE {_DB}")
    try:
        yield pg_target
    finally:
        with contextlib.suppress(Exception):
            _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")


def _project(tmp_path: Path, drift_db, migration: str) -> Path:
    """A confiture project laid out the way ``confiture build --env`` expects."""
    root = tmp_path / "app"
    (root / "db" / "schema").mkdir(parents=True)
    (root / "db" / "migrations").mkdir(parents=True)
    (root / "db" / "environments").mkdir(parents=True)

    (root / "db" / "schema" / "010_core.sql").write_text(_DDL)
    (root / "db" / "migrations" / "20260907000000_create_widget.py").write_text(
        migration
    )
    (root / "db" / "environments" / "production.yaml").write_text(
        f"name: production\ndatabase_url: {drift_db.dsn(_DB)}\ninclude_dirs:\n  - db/schema\n"
    )
    return root


def _gate(project: Path):
    return check_schema_drift(
        project_dir=project,
        confiture_config=project / "db" / "environments" / "production.yaml",
        checks=["live-drift"],
    )


def _migrate(project: Path):
    return migrate_up(
        project / "db" / "environments" / "production.yaml",
        migrations_dir=project / "db" / "migrations",
        project_dir=project,
    )


class TestTheFailureItCloses:
    def test_the_migration_itself_reports_success(self, tmp_path, drift_db) -> None:
        """The premise: nothing in the migration path notices.

        If this ever fails, the reproduction has stopped being the #395 shape —
        most likely because the function body became `LANGUAGE sql`, which
        PostgreSQL *does* validate at creation.
        """
        project = _project(tmp_path, drift_db, _MIGRATION_MISSING_COLUMN)

        result = _migrate(project)

        assert result.success is True
        assert result.steps_applied == 1

    def test_the_gate_catches_what_the_migration_missed(
        self, tmp_path, drift_db
    ) -> None:
        project = _project(tmp_path, drift_db, _MIGRATION_MISSING_COLUMN)
        assert _migrate(project).success is True

        result = _gate(project)

        assert result.ran, result.error
        assert not result.passed
        assert result.exit_code == 1
        assert [item.object_name for item in result.critical] == [
            "core.tb_widget.label"
        ]

    def test_the_function_really_is_broken(self, tmp_path, drift_db) -> None:
        """The gate is right: calling the function fails, exactly as reported.

        Without this the suite would only prove the gate and the DDL disagree,
        not that the disagreement is a real defect.
        """
        project = _project(tmp_path, drift_db, _MIGRATION_MISSING_COLUMN)
        assert _migrate(project).success is True

        with (
            psycopg.connect(drift_db.dsn(_DB)) as conn,
            pytest.raises(psycopg.errors.UndefinedColumn),
        ):
            conn.execute("SELECT core.fn_widget_label(1)")


class TestACleanDeployPasses:
    def test_a_matching_multi_schema_database_reports_no_drift(
        self, tmp_path, drift_db
    ) -> None:
        """Schema-qualified DDL, live built from it: the confiture 1.0.0 fix.

        On 0.46 this reported ``CRITICAL MISSING_TABLE core`` — the schema name
        parsed as the table name — so the gate would have failed closed on every
        deploy of a multi-schema project. It is what the ``>=1.0.0`` floor buys.
        """
        project = _project(tmp_path, drift_db, _MIGRATION_COMPLETE)
        assert _migrate(project).success is True

        result = _gate(project)

        assert result.ran, result.error
        assert result.passed, result.summary()
        assert result.critical == ()

    def test_the_check_leaves_no_artefact_in_the_checkout(
        self, tmp_path, drift_db
    ) -> None:
        project = _project(tmp_path, drift_db, _MIGRATION_COMPLETE)
        assert _migrate(project).success is True
        before = sorted(p.relative_to(project) for p in project.rglob("*"))

        _gate(project)

        assert sorted(p.relative_to(project) for p in project.rglob("*")) == before


class TestWhyItRunsAfterTheMigration:
    def test_before_the_migration_a_legitimate_deploy_fails_closed(
        self, tmp_path, drift_db
    ) -> None:
        """The measurement the placement rests on, pinned so a move re-fails.

        With the migration pending — an ordinary deploy, nothing wrong with it —
        every table the DDL declares is "missing from the database", and
        ``MISSING_TABLE`` is CRITICAL. Running the gate here would abort every
        deploy that carries a table-adding migration.
        """
        project = _project(tmp_path, drift_db, _MIGRATION_COMPLETE)

        before = _gate(project)

        assert before.ran, before.error
        assert not before.passed
        assert "core.tb_widget" in {item.object_name for item in before.critical}

        # ...and the same project, same gate, after the migration: clean.
        assert _migrate(project).success is True
        assert _gate(project).passed

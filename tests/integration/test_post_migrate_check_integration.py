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

#: The one signature the checkout declares for ``core.fn_seen`` — in ``core``,
#: like every routine a FraiseQL project has, and therefore invisible to a
#: ``--check-signatures`` that was never told to look outside ``public`` (#408).
_ROUTINE_DDL = """CREATE OR REPLACE FUNCTION core.fn_seen(p_at TIMESTAMP WITH TIME ZONE)
    RETURNS INT LANGUAGE sql AS $$ SELECT 1 $$;
"""

#: A migration that changes a parameter type the way ``CREATE OR REPLACE``
#: invites: PostgreSQL does not replace the function, it adds an overload, and
#: the old one stays live and callable. Both statements succeed and the
#: migration reports success.
_MIGRATION_STALE_OVERLOAD = '''from confiture.models.migration import Migration


class CreateWidget(Migration):
    version = "20260907000000"
    name = "create_widget"

    def up(self):
        self.connection.execute("CREATE SCHEMA IF NOT EXISTS core")
        self.connection.execute(
            "CREATE TABLE core.tb_widget (id BIGINT PRIMARY KEY, "
            "serial TEXT NOT NULL, label TEXT)"
        )
        self.connection.execute(
            """CREATE OR REPLACE FUNCTION core.fn_seen(p_at TIMESTAMP)
            RETURNS INT LANGUAGE sql AS $$ SELECT 1 $$"""
        )
        self.connection.execute(
            """CREATE OR REPLACE FUNCTION core.fn_seen(p_at TIMESTAMPTZ)
            RETURNS INT LANGUAGE sql AS $$ SELECT 1 $$"""
        )

    def down(self):
        self.connection.execute("DROP SCHEMA core CASCADE")
'''

#: The same migration that got the parameter type right the first time: one
#: overload, and it is the one the DDL declares.
_MIGRATION_ONE_OVERLOAD = '''from confiture.models.migration import Migration


class CreateWidget(Migration):
    version = "20260907000000"
    name = "create_widget"

    def up(self):
        self.connection.execute("CREATE SCHEMA IF NOT EXISTS core")
        self.connection.execute(
            "CREATE TABLE core.tb_widget (id BIGINT PRIMARY KEY, "
            "serial TEXT NOT NULL, label TEXT)"
        )
        self.connection.execute(
            """CREATE OR REPLACE FUNCTION core.fn_seen(p_at TIMESTAMPTZ)
            RETURNS INT LANGUAGE sql AS $$ SELECT 1 $$"""
        )

    def down(self):
        self.connection.execute("DROP SCHEMA core CASCADE")
'''


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


def _project(
    tmp_path: Path, drift_db, migration: str, extra_ddl: dict[str, str] | None = None
) -> Path:
    """A confiture project laid out the way ``confiture build --env`` expects.

    *extra_ddl* maps a filename under ``db/schema/`` to its contents, for the
    cases that need the build itself to have something to say (#401).
    """
    root = tmp_path / "app"
    (root / "db" / "schema").mkdir(parents=True)
    (root / "db" / "migrations").mkdir(parents=True)
    (root / "db" / "environments").mkdir(parents=True)

    (root / "db" / "schema" / "010_core.sql").write_text(_DDL)
    for name, body in (extra_ddl or {}).items():
        (root / "db" / "schema" / name).write_text(body)
    (root / "db" / "migrations" / "20260907000000_create_widget.py").write_text(
        migration
    )
    (root / "db" / "environments" / "production.yaml").write_text(
        f"name: production\ndatabase_url: {drift_db.dsn(_DB)}\ninclude_dirs:\n  - db/schema\n"
    )
    return root


def _gate(project: Path, checks: list[str] | None = None):
    return check_schema_drift(
        project_dir=project,
        confiture_config=project / "db" / "environments" / "production.yaml",
        checks=checks or ["live-drift"],
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


class TestTheSignaturesCheckOutsidePublic:
    """#408, against a real confiture: the check had never been able to fire.

    ``--schemas`` defaults to ``public`` and this gate sent a bare
    ``--check-signatures``, so for any project that keeps its routines in
    ``core``/``app``/``tenant`` — every FraiseQL project — the check inspected a
    schema the project does not use and reported a clean database.

    ``tests/test_drift_gate.py`` pins the argv.  These execute it: the stale
    overload below is in ``core``, so a gate that stops deriving the schemas
    goes quietly back to passing and this fails.
    """

    def test_a_stale_overload_outside_public_fails_the_gate(
        self, tmp_path, drift_db
    ) -> None:
        project = _project(
            tmp_path,
            drift_db,
            _MIGRATION_STALE_OVERLOAD,
            extra_ddl={"020_routines.sql": _ROUTINE_DDL},
        )
        assert _migrate(project).success is True

        result = _gate(project, checks=["signatures"])

        assert result.ran, result.error
        assert not result.passed, result.summary()
        assert result.exit_code == 1
        assert [item.object_name for item in result.critical] == [
            "core.fn_seen(timestamp without time zone)"
        ]

    def test_the_verdict_carries_the_statement_that_fixes_it(
        self, tmp_path, drift_db
    ) -> None:
        """confiture computes the ``DROP FUNCTION``; it is worth nothing unread."""
        project = _project(
            tmp_path,
            drift_db,
            _MIGRATION_STALE_OVERLOAD,
            extra_ddl={"020_routines.sql": _ROUTINE_DDL},
        )
        assert _migrate(project).success is True

        summary = _gate(project, checks=["signatures"]).summary()

        assert "DROP FUNCTION core.fn_seen(timestamp without time zone);" in summary

    def test_the_migration_itself_reports_success(self, tmp_path, drift_db) -> None:
        """The premise again: nothing on the migration path notices the overload.

        ``CREATE OR REPLACE`` with a changed parameter type is a *create*, not a
        replace, and PostgreSQL says nothing about the function it left behind.
        """
        project = _project(
            tmp_path,
            drift_db,
            _MIGRATION_STALE_OVERLOAD,
            extra_ddl={"020_routines.sql": _ROUTINE_DDL},
        )

        assert _migrate(project).success is True

        with psycopg.connect(drift_db.dsn(_DB)) as conn:
            live = conn.execute(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n "
                "ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'core' AND p.proname = 'fn_seen'"
            ).fetchone()
        assert live == (2,)

    def test_the_declared_overload_alone_passes(self, tmp_path, drift_db) -> None:
        """The gate stays quiet when the database matches what the DDL declares.

        Without this, pointing the check at more schemas could be "caught" by
        failing everywhere, and the suite would not know the difference.
        """
        project = _project(
            tmp_path,
            drift_db,
            _MIGRATION_ONE_OVERLOAD,
            extra_ddl={"020_routines.sql": _ROUTINE_DDL},
        )
        assert _migrate(project).success is True

        result = _gate(project, checks=["signatures"])

        assert result.ran, result.error
        assert result.passed, result.summary()
        assert result.critical == ()


#: Objects the checkout declares beyond its tables — a view, a routine and a
#: trigger. Until confiture 1.11.0 a database missing any of them was exit 0
#: with ``drift_items: []``, so the gate passed a deploy whose migration had
#: simply not created them.
_DDL_OBJECTS = """CREATE OR REPLACE FUNCTION core.fn_label(p_id BIGINT)
    RETURNS TEXT LANGUAGE sql AS $$ SELECT 'x' $$;

CREATE VIEW core.v_widget AS SELECT id, serial FROM core.tb_widget;

CREATE OR REPLACE FUNCTION core.fn_touch()
    RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$;

CREATE TRIGGER trg_touch BEFORE UPDATE ON core.tb_widget
    FOR EACH ROW EXECUTE FUNCTION core.fn_touch();
"""


class TestObjectsTheMigrationNeverCreated:
    """confiture 1.11.0 grades a missing view, routine or trigger CRITICAL.

    Measured on one database, same DDL, ``--check-live-drift``: on 1.10.1 each
    of these is exit 0 with ``drift_items: []``; on 1.11.0 each is exit 1 with a
    critical item naming the object. That is a **new deploy-failing path** on a
    gate that runs after every migration, which is why it is pinned here rather
    than trusted to the release notes.

    The behaviour arrives in confiture 1.11.0. ``pyproject.toml``'s floor is
    ``>=1.0.0`` and does not promise it — the floor says the gate *works*, the
    lock says what it *reports*. If the lock is ever moved below 1.11.0 these
    fail, which is the intended alarm.
    """

    def _gate_without_objects(self, tmp_path, drift_db):
        """The DDL declares view/routine/trigger; the migration creates none."""
        project = _project(
            tmp_path,
            drift_db,
            _MIGRATION_COMPLETE,
            extra_ddl={"020_objects.sql": _DDL_OBJECTS},
        )
        assert _migrate(project).success is True
        return _gate(project)

    def test_the_migration_itself_reports_success(self, tmp_path, drift_db) -> None:
        """The premise: nothing before the gate notices the objects are absent."""
        project = _project(
            tmp_path,
            drift_db,
            _MIGRATION_COMPLETE,
            extra_ddl={"020_objects.sql": _DDL_OBJECTS},
        )

        result = _migrate(project)

        assert result.success is True
        assert result.steps_applied == 1

    def test_a_missing_view_routine_and_trigger_all_fail_the_gate(
        self, tmp_path, drift_db
    ) -> None:
        result = self._gate_without_objects(tmp_path, drift_db)

        assert result.ran, result.error
        assert not result.passed, result.summary()
        assert result.exit_code == 1
        kinds = {item.kind for item in result.critical}
        assert {"missing_view", "missing_routine", "missing_trigger"} <= kinds, (
            result.summary()
        )

    def test_each_missing_object_is_named(self, tmp_path, drift_db) -> None:
        """A verdict that says "3 critical items" and nothing else is unactionable."""
        result = self._gate_without_objects(tmp_path, drift_db)

        named = {item.object_name for item in result.critical}
        assert "core.v_widget" in named
        assert any(n.startswith("core.fn_label") for n in named), named
        assert any("trg_touch" in n for n in named), named

    def test_a_database_that_has_them_all_passes(self, tmp_path, drift_db) -> None:
        """The control: these verdicts must discriminate, not fire on everything."""
        migration = _MIGRATION_COMPLETE.replace(
            "    def down(self):",
            '''        self.connection.execute(
            """CREATE OR REPLACE FUNCTION core.fn_label(p_id BIGINT)
            RETURNS TEXT LANGUAGE sql AS $$ SELECT 'x' $$"""
        )
        self.connection.execute(
            "CREATE VIEW core.v_widget AS SELECT id, serial FROM core.tb_widget"
        )
        self.connection.execute(
            """CREATE OR REPLACE FUNCTION core.fn_touch()
            RETURNS TRIGGER LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"""
        )
        self.connection.execute(
            """CREATE TRIGGER trg_touch BEFORE UPDATE ON core.tb_widget
            FOR EACH ROW EXECUTE FUNCTION core.fn_touch()"""
        )

    def down(self):''',
        )
        project = _project(
            tmp_path, drift_db, migration, extra_ddl={"020_objects.sql": _DDL_OBJECTS}
        )
        assert _migrate(project).success is True

        result = _gate(project)

        assert result.ran, result.error
        assert result.passed, result.summary()


class TestTheBuildsOwnDiagnostics:
    """#401: what the build said reaches the verdict, from a real confiture.

    ``tests/test_drift_gate.py`` asserts the codes against a double built from
    a recorded envelope.  These execute the build, so a confiture that renames
    a key, moves a code or stops filling the channel fails here rather than
    going quietly back to the behaviour #401 was filed about.

    Neither test asserts the *verdict*.  Both projects deliberately hand the
    build DDL that is wrong in some way, so what live-drift then makes of it is
    confiture's business; what is being proved is that the sentence explaining
    it survived.
    """

    def test_a_duplicate_definition_reaches_the_verdict(
        self, tmp_path, drift_db
    ) -> None:
        """``build_001`` — an object two of the build's files define.

        Reachable only because the gate passes ``--warn-duplicates``: measured
        against confiture 1.6.0, a plain ``--schema-only`` build over this same
        tree reports ``duplicates: []``.
        """
        project = _project(
            tmp_path,
            drift_db,
            _MIGRATION_COMPLETE,
            extra_ddl={
                "020_again.sql": "CREATE TABLE core.tb_widget (id BIGINT PRIMARY KEY);\n"
            },
        )
        assert _migrate(project).success is True

        result = _gate(project)

        codes = [note.code for note in result.build_notes]
        assert "build_001" in codes, result.summary()
        note = next(n for n in result.build_notes if n.code == "build_001")
        assert "core.tb_widget" in note.message
        assert "010_core.sql" in note.message
        assert "020_again.sql" in note.message
        assert "build_001" in result.summary()

    def test_a_file_the_parser_cannot_read_reaches_the_verdict(
        self, tmp_path, drift_db
    ) -> None:
        """``SCHEMA_206`` — a file that went into the schema unchecked.

        The build exits 0 and writes a complete schema; the only signal that
        one of its files was never looked at is this note.
        """
        project = _project(
            tmp_path,
            drift_db,
            _MIGRATION_COMPLETE,
            extra_ddl={"030_broken.sql": "CREATE TABLE core.tb_truncated (id BIGINT\n"},
        )
        assert _migrate(project).success is True

        result = _gate(project)

        codes = [note.code for note in result.build_notes]
        assert "SCHEMA_206" in codes, result.summary()
        note = next(n for n in result.build_notes if n.code == "SCHEMA_206")
        assert note.severity == "warning"
        assert "030_broken.sql" in str(note.file)

    def test_an_ordinary_build_says_nothing(self, tmp_path, drift_db) -> None:
        """The channel is quiet when there is nothing to say — no per-deploy noise."""
        project = _project(tmp_path, drift_db, _MIGRATION_COMPLETE)
        assert _migrate(project).success is True

        result = _gate(project)

        assert result.passed, result.summary()
        assert result.build_notes == ()

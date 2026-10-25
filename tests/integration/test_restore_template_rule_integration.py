"""#481 end to end: a restore that fails at step 10 rolls back what it applied.

A real ``pg_dump`` of a confiture-managed database, a real restore, a real
``migrate up`` of one pending migration, then a table-count floor that cannot be
met. Step 8 decides from confiture's own preflight against the restored
database, so the template rule is exercised on the wire it reads, not on a
fixture.

Skipped unless a PostgreSQL with ``createdb`` privilege and the client tools are
reachable — the shared ``pg_target`` fixture in ``conftest.py`` decides that.
"""

from __future__ import annotations

import contextlib
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import PgTarget

pytestmark = pytest.mark.integration

psycopg = pytest.importorskip("psycopg")

_SRC = "fraisier_it_tmpl_src"
_DST = "fraisier_it_tmpl_dst"
_TEMPLATE = f"template_{_DST}"

_APPLIED = {
    "001_create_t.up.sql": "CREATE TABLE t (id int PRIMARY KEY, v int);\n",
    "001_create_t.down.sql": "DROP TABLE t;\n",
}

#: The one migration each run applies, by how it can be undone.
_PENDING = {
    "reversible": {
        "002_add_w.up.sql": "ALTER TABLE t ADD COLUMN w int;\n",
        "002_add_w.down.sql": "ALTER TABLE t DROP COLUMN w;\n",
    },
    "broken-down": {
        "002_add_w.up.sql": "ALTER TABLE t ADD COLUMN w int;\n",
        "002_add_w.down.sql": "ALTER TABLE t DROP COLUMN no_such_column;\n",
    },
    "python": {
        "002_add_u.py": (
            "from confiture.models.migration import Migration\n\n\n"
            "class AddU(Migration):\n"
            '    version = "002"\n'
            '    name = "add_u"\n\n'
            "    def up(self) -> None:\n"
            '        self.execute("CREATE TABLE u (id int)")\n\n'
            "    def down(self) -> None:\n"
            '        self.execute("DROP TABLE u")\n'
        ),
    },
    "python-empty-down": {
        "002_add_u.py": (
            "from confiture.models.migration import Migration\n\n\n"
            "class AddU(Migration):\n"
            '    version = "002"\n'
            '    name = "add_u"\n\n'
            "    def up(self) -> None:\n"
            '        self.execute("CREATE TABLE u (id int)")\n\n'
            "    def down(self) -> None:\n"
            "        pass\n"
        ),
    },
}


def _exec(db: str, target: PgTarget, *statements: str) -> None:
    with psycopg.connect(target.dsn(db), autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


def _drop_all(target: PgTarget) -> None:
    for db in (_TEMPLATE, _SRC, _DST):
        with contextlib.suppress(Exception):
            _exec(
                "postgres",
                target,
                f"UPDATE pg_database SET datistemplate = false WHERE datname = '{db}'",
            )
        with contextlib.suppress(Exception):
            _exec("postgres", target, f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


def _confiture(*args: str) -> None:
    from fraisier.dbops.confiture_executable import confiture_executable

    proc = subprocess.run(
        [confiture_executable(), *args], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _database_exists(target: PgTarget, name: str) -> bool:
    with psycopg.connect(target.dsn("postgres")) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
    return row is not None


def _columns(target: PgTarget) -> list[str]:
    with psycopg.connect(target.dsn(_DST)) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 't' ORDER BY ordinal_position"
        ).fetchall()
    return [r[0] for r in rows]


def _ledger(target: PgTarget) -> list[str]:
    with psycopg.connect(target.dsn(_DST)) as conn:
        rows = conn.execute(
            "SELECT version FROM tb_confiture ORDER BY version"
        ).fetchall()
    return [r[0] for r in rows]


@pytest.fixture
def staging(pg_target, tmp_path):
    """A dump of a database with 001 applied; returns a builder per pending case."""
    _drop_all(pg_target)
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for name, body in _APPLIED.items():
        (migrations / name).write_text(body)
    src_config = tmp_path / "src.yaml"
    src_config.write_text(f"database_url: {pg_target.dsn(_SRC)}\n")
    backups = tmp_path / "backups"
    backups.mkdir()
    try:
        _exec("postgres", pg_target, f"CREATE DATABASE {_SRC}")
        _confiture(
            "migrate",
            "up",
            "--config",
            str(src_config),
            "--migrations-dir",
            str(migrations),
        )
        _exec(_SRC, pg_target, "INSERT INTO t VALUES (1, 10), (2, 20)")
        subprocess.run(
            [
                "pg_dump",
                "-Fc",
                "-d",
                pg_target.dsn(_SRC),
                "-f",
                str(backups / "src.dump"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        def build(case: str, **config) -> tuple:
            from fraisier.config.schema import PreflightConfig
            from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy

            for name, body in _PENDING[case].items():
                (migrations / name).write_text(body)
            dst_config = tmp_path / "dst.yaml"
            dst_config.write_text(f"database_url: {pg_target.dsn(_DST)}\n")
            strategy = RestoreMigrateStrategy(
                RestoreConfig(
                    db_name=_DST,
                    backup_dir=backups,
                    # The archive replay (step 2.5) is not what this is about.
                    preflight=PreflightConfig(enabled=False),
                    **config,
                ),
                admin_url=pg_target.dsn("postgres"),
            )
            return strategy, dst_config, migrations

        yield pg_target, build
    finally:
        _drop_all(pg_target)


def _fail_at_step_10(strategy, config: Path, migrations: Path) -> int:
    from fraisier.errors import DatabaseError

    with pytest.raises(DatabaseError, match="Table count validation failed") as exc:
        strategy.execute(config, migrations_dir=migrations)
    assert exc.value.steps_applied is not None
    return exc.value.steps_applied


def test_auto_skips_the_template_and_migrate_down_undoes_the_run(staging):
    target, build = staging
    strategy, config, migrations = build(
        "reversible", create_template=True, min_tables=99
    )

    steps = _fail_at_step_10(strategy, config, migrations)

    assert steps == 1
    assert _columns(target) == ["id", "v", "w"]
    assert not _database_exists(target, _TEMPLATE)

    result = strategy.rollback(config, migrations_dir=migrations, steps=steps)

    assert result.success, result.errors
    assert _columns(target) == ["id", "v"]
    assert _ledger(target) == ["001"]


def test_a_down_that_fails_raises_and_leaves_the_schema(staging):
    """confiture raises on a failing down; the deployer reports ROLLBACK_FAILED.

    That mapping is pinned in ``tests/test_restore_rollback_without_template.py``.
    """
    from confiture.exceptions import ConfiturError

    target, build = staging
    strategy, config, migrations = build(
        "broken-down", create_template=True, min_tables=99
    )
    steps = _fail_at_step_10(strategy, config, migrations)

    with pytest.raises(ConfiturError, match="Failed to rollback migration 002"):
        strategy.rollback(config, migrations_dir=migrations, steps=steps)

    assert _columns(target) == ["id", "v", "w"]
    assert not _database_exists(target, _TEMPLATE)


def test_auto_takes_the_template_for_a_python_migration(staging):
    target, build = staging
    strategy, config, migrations = build("python", create_template=True, min_tables=99)

    steps = _fail_at_step_10(strategy, config, migrations)

    assert _database_exists(target, _TEMPLATE)
    result = strategy.rollback(config, migrations_dir=migrations, steps=steps)
    assert result.success, result.errors
    with psycopg.connect(target.dsn(_DST)) as conn:
        assert conn.execute("SELECT to_regclass('public.u')").fetchone()[0] is None
    assert _ledger(target) == ["001"]


def test_always_takes_the_template_for_a_reversible_set(staging):
    target, build = staging
    strategy, config, migrations = build("reversible", template="always", min_tables=99)

    steps = _fail_at_step_10(strategy, config, migrations)

    assert _database_exists(target, _TEMPLATE)
    result = strategy.rollback(config, migrations_dir=migrations, steps=steps)
    assert result.success, result.errors
    assert _columns(target) == ["id", "v"]


def test_never_takes_no_template_for_a_python_migration(staging):
    target, build = staging
    strategy, config, migrations = build("python", template="never", min_tables=99)

    _fail_at_step_10(strategy, config, migrations)

    assert not _database_exists(target, _TEMPLATE)


def _table_u_exists(target: PgTarget) -> bool:
    with psycopg.connect(target.dsn(_DST)) as conn:
        return conn.execute("SELECT to_regclass('public.u')").fetchone()[0] is not None


def test_auto_takes_the_template_for_an_empty_python_down(staging):
    """confiture 1.40 reports it as PFLIGHT_DOWN_EMPTY, so `reversible` is false."""
    target, build = staging
    strategy, config, migrations = build(
        "python-empty-down", create_template=True, min_tables=99
    )

    steps = _fail_at_step_10(strategy, config, migrations)

    assert _database_exists(target, _TEMPLATE)
    result = strategy.rollback(config, migrations_dir=migrations, steps=steps)
    assert result.success, result.errors
    assert not _table_u_exists(target)
    assert _ledger(target) == ["001"]


def test_never_refuses_to_roll_back_an_empty_python_down(staging):
    """#484: confiture < 1.40 ran the no-op, deleted 002's ledger row and called
    it success, leaving table u behind. 1.40 refuses before running anything,
    so the deployer reports ROLLBACK_FAILED instead of a rollback that lied."""
    from confiture.exceptions import RollbackError

    target, build = staging
    strategy, config, migrations = build(
        "python-empty-down", template="never", min_tables=99
    )
    steps = _fail_at_step_10(strategy, config, migrations)

    with pytest.raises(RollbackError, match="no usable down for 002") as exc:
        strategy.rollback(config, migrations_dir=migrations, steps=steps)

    assert exc.value.error_code == "ROLLBACK_600"
    assert _ledger(target) == ["001", "002"]
    assert _table_u_exists(target)

"""Deploying a migration that still carries a backfill placeholder.

confiture 1.42.0's ``migrate diff --generate`` writes a change that existing
rows could refuse around a ``-- confiture:backfill <target>`` placeholder, for
the author to replace with the statements that fix those rows. ``migrate up``
refuses a file that still carries one, with ``VALID_010``, before anything
applies, and no flag overrides it.

The refusal is a raw ``confiture.exceptions.ValidationError``. Unconverted it
sails past ``_run_strategy``'s ``MigrationError`` handler, as ``VALID_002`` did
(#398), and its hint names a confiture CLI fraisier never runs. A real database
and the real confiture are used because the gate lives upstream.
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

_DB = "fraisier_it_backfill"

_PLACEHOLDER = """ALTER TABLE tb_post ADD COLUMN code text;
-- confiture:backfill tb_post.code
-- Replace this block with the statements that fix the rows where code is null.
ALTER TABLE tb_post ALTER COLUMN code SET NOT NULL;
"""
_FILLED = _PLACEHOLDER.replace(
    "-- confiture:backfill tb_post.code\n",
    "-- confiture:backfill none\nUPDATE tb_post SET code = 'x';\n",
)


def _exec(db: str, target, *statements: str) -> None:
    with psycopg.connect(target.dsn(db), autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


@pytest.fixture
def backfill_db(pg_target):
    with contextlib.suppress(Exception):
        _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")
    _exec("postgres", pg_target, f"CREATE DATABASE {_DB}")
    _exec(
        _DB,
        pg_target,
        "CREATE TABLE tb_post (pk_post bigint PRIMARY KEY)",
        "INSERT INTO tb_post VALUES (1), (2)",
    )
    try:
        yield pg_target
    finally:
        with contextlib.suppress(Exception):
            _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")


def _migrate(tmp_path: Path, target, up: str, **kwargs):
    root = tmp_path / "app"
    migrations = root / "db" / "migrations"
    migrations.mkdir(parents=True)
    (root / "db" / "environments").mkdir(parents=True)
    (migrations / "20261028000000_add_code.up.sql").write_text(up)
    (migrations / "20261028000000_add_code.down.sql").write_text(
        "ALTER TABLE tb_post DROP COLUMN code;\n"
    )
    config = root / "db" / "environments" / "production.yaml"
    config.write_text(
        f"name: production\ndatabase_url: {target.dsn(_DB)}\ninclude_dirs: []\n"
    )
    return migrate_up(config, migrations_dir=migrations, project_dir=root, **kwargs)


def _has_code(target) -> bool:
    with psycopg.connect(target.dsn(_DB)) as conn:
        row = conn.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'tb_post' AND column_name = 'code'"
        ).fetchone()
    return bool(row and row[0])


@pytest.mark.parametrize("allow_destructive", [False, True])
def test_a_placeholder_is_a_fraisier_error_and_nothing_applies(
    tmp_path, backfill_db, allow_destructive: bool
) -> None:
    with pytest.raises(MigrationError) as excinfo:
        _migrate(
            tmp_path, backfill_db, _PLACEHOLDER, allow_destructive=allow_destructive
        )

    assert excinfo.value.steps_applied == 0
    assert not _has_code(backfill_db)
    message = str(excinfo.value)
    assert "20261028000000_add_code.up.sql (tb_post.code)" in message
    assert "-- confiture:backfill none" in message


def test_the_control_a_filled_placeholder_applies(tmp_path, backfill_db) -> None:
    """Otherwise the test above might be measuring the migration, not the gate."""
    result = _migrate(tmp_path, backfill_db, _FILLED)

    assert result.steps_applied == 1
    assert _has_code(backfill_db)

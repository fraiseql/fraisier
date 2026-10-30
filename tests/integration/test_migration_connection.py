"""The migration's own connection reads no ambient ``PG*``.

confiture opens it in process from the config's ``database_url``, so libpq
fills every keyword the URL leaves out from ``os.environ``: the deploy's
schema change ran with the deploy user's ``PGOPTIONS``. fraisier hands
confiture a connection factory that goes through ``PgConn.conninfo()``.
Each test shows a bare connect following the variable (the control), then
the migration not following it.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from fraisier.dbops.confiture import has_pending, migrate_down, migrate_up

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

psycopg = pytest.importorskip("psycopg")

_DB = "fraisier_it_migration_conn"
_AMBIENT = "-c search_path=public,acme_ambient"


def _exec(db: str, target, *statements: str) -> None:
    with psycopg.connect(target.dsn(db), autocommit=True) as conn:
        for statement in statements:
            conn.execute(statement)


def _seen(target) -> list[tuple[str, str]]:
    with psycopg.connect(target.dsn(_DB)) as conn:
        return conn.execute("SELECT step, sp FROM public.tb_seen ORDER BY 1").fetchall()


@pytest.fixture
def target(pg_target):
    with contextlib.suppress(Exception):
        _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")
    _exec("postgres", pg_target, f"CREATE DATABASE {_DB}")
    _exec(_DB, pg_target, "CREATE TABLE public.tb_seen (step text, sp text)")
    try:
        yield pg_target
    finally:
        with contextlib.suppress(Exception):
            _exec("postgres", pg_target, f"DROP DATABASE IF EXISTS {_DB} WITH (FORCE)")


@pytest.fixture
def project(tmp_path: Path, target) -> tuple[Path, Path]:
    root = tmp_path / "app"
    migrations = root / "db" / "migrations"
    migrations.mkdir(parents=True)
    (root / "db" / "environments").mkdir(parents=True)
    record = "INSERT INTO public.tb_seen SELECT '{}', current_setting('search_path');\n"
    (migrations / "20261029000000_seen.up.sql").write_text(record.format("up"))
    (migrations / "20261029000000_seen.down.sql").write_text(record.format("down"))
    config = root / "db" / "environments" / "production.yaml"
    config.write_text(
        f"name: production\ndatabase_url: {target.dsn(_DB)}\ninclude_dirs: []\n"
    )
    return config, migrations


def test_the_control_follows_the_ambient_variable(target, monkeypatch):
    monkeypatch.setenv("PGOPTIONS", _AMBIENT)

    with psycopg.connect(target.dsn(_DB)) as conn:
        sp = conn.execute("SELECT current_setting('search_path')").fetchone()[0]

    assert sp == "public,acme_ambient"


def test_migrate_up_and_down_ignore_an_ambient_pgoptions(target, project, monkeypatch):
    config, migrations = project
    monkeypatch.setenv("PGOPTIONS", _AMBIENT)

    migrate_up(config, migrations_dir=migrations)
    migrate_down(config, migrations_dir=migrations, steps=1)

    assert all("acme_ambient" not in sp for _, sp in _seen(target))
    assert [step for step, _ in _seen(target)] == ["down", "up"]


def test_a_refused_environment_stops_it_before_anything_applies(
    target, project, monkeypatch
):
    config, migrations = project
    monkeypatch.setenv("PGTZ", "Asia/Tokyo")

    with pytest.raises(ValueError, match="PGTZ is set in the environment"):
        has_pending(config, migrations_dir=migrations)
    with pytest.raises(ValueError, match="PGTZ is set in the environment"):
        migrate_up(config, migrations_dir=migrations)
    monkeypatch.delenv("PGTZ")
    assert _seen(target) == []

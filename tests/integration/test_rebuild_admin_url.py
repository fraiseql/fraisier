"""A rebuild through the ``admin_url`` ``fraisier init`` writes.

The development template's ``admin_url`` is ``postgresql:///postgres``: the
socket in libpq's default directory, as the deploy OS user. The rebuild drops
and recreates the database through it, so the role it connects as needs
``CREATEDB``. Without it, a role that owns the database could drop it and then
fail to create it; the rebuild refuses before the drop instead.
"""

from __future__ import annotations

import textwrap
import uuid
from typing import TYPE_CHECKING
from urllib.parse import quote

import psycopg
import pytest
import yaml

from fraisier.dbops._pgconn import PgConn
from fraisier.errors import DatabaseError
from fraisier.init_templates import django_template
from fraisier.strategies import RebuildStrategy

from .conftest import PgTarget

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def _template_admin_url() -> str:
    (fraise,) = yaml.safe_load(django_template())["fraises"].values()
    return fraise["environments"]["development"]["database"]["admin_url"]


def _project(tmp_path: Path, database_url: str) -> tuple[Path, Path]:
    """A confiture project whose schema is one table."""
    body = textwrap.dedent(f"""\
        name: test
        database_url: "{database_url}"
        include_dirs:
          - "{tmp_path / "db" / "0_schema"}"
        """)
    (tmp_path / "confiture.yaml").write_text(body)
    env_dir = tmp_path / "db" / "environments"
    env_dir.mkdir(parents=True)
    (env_dir / "test.yaml").write_text(body)
    schema = tmp_path / "db" / "0_schema" / "01_public"
    schema.mkdir(parents=True)
    (schema / "011_tb_acme.sql").write_text(
        "CREATE TABLE public.tb_acme (id serial PRIMARY KEY);\n"
    )
    migrations = tmp_path / "db" / "migrations"
    migrations.mkdir(parents=True)
    return tmp_path / "confiture.yaml", migrations


def _drop(admin: str, db_name: str) -> None:
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)")  # ty: ignore[no-matching-overload]


class TestTheTemplatesAdminUrl:
    @pytest.fixture
    def template_admin_url(self, pg_target: PgTarget) -> str:
        """The template's URL, where it reaches a role that may create databases."""
        url = _template_admin_url()
        try:
            with psycopg.connect(PgConn.parse(url).conninfo(), connect_timeout=3) as c:
                row = c.execute(
                    "SELECT rolcreatedb OR rolsuper FROM pg_roles "
                    "WHERE rolname = current_user"
                ).fetchone()
        except psycopg.OperationalError:
            reason = "no PostgreSQL on libpq's default socket directory here"
            pytest.skip(reason)  # ty: ignore[too-many-positional-arguments]
        if not (row and row[0]):
            reason = "the OS user's PostgreSQL role may not create databases"
            pytest.skip(reason)  # ty: ignore[too-many-positional-arguments]
        return url

    def test_a_rebuild_through_it_creates_the_database(
        self, template_admin_url, tmp_path
    ):
        db_name = f"fraisier_rebuild_{uuid.uuid4().hex[:10]}"
        database_url = f"postgresql:///{db_name}"
        config, migrations = _project(tmp_path, database_url)
        try:
            result = RebuildStrategy(admin_url=template_admin_url).execute(
                config, migrations_dir=migrations, database_url=database_url
            )

            assert result.success
            with psycopg.connect(PgConn.parse(database_url).conninfo()) as conn:
                row = conn.execute("SELECT to_regclass('public.tb_acme')").fetchone()
            assert row
            assert row[0]
        finally:
            # Created as the OS user, so dropped as it: the suite's own role
            # may not own it.
            _drop(PgConn.parse(template_admin_url).conninfo(), db_name)


class TestARoleWithoutCreatedb:
    @pytest.fixture
    def role(self, pg_target: PgTarget):
        """A login role without CREATEDB that owns a database with a table."""
        admin = pg_target.dsn("postgres")
        role = f"acme_nocreatedb_{uuid.uuid4().hex[:8]}"
        password = uuid.uuid4().hex
        db_name = f"fraisier_owned_{uuid.uuid4().hex[:10]}"
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(  # ty: ignore[no-matching-overload]
                f"CREATE ROLE {role} LOGIN NOCREATEDB PASSWORD '{password}'"
            )
            # Giving it a database needs SET ROLE to it (PostgreSQL 16), which
            # a CREATEROLE role that is not a superuser does not get by itself.
            conn.execute(f"GRANT {role} TO CURRENT_USER")  # ty: ignore[no-matching-overload]
            conn.execute(f"CREATE DATABASE {db_name} OWNER {role}")  # ty: ignore[no-matching-overload]
        if pg_target.over_socket:
            host = quote(pg_target.host, safe="")
            url = f"postgresql://{role}:{password}@/{{}}?host={host}"
        else:
            as_role = PgTarget(pg_target.host, pg_target.port, role, password)
            url = as_role.dsn("{}")
        with psycopg.connect(url.format(db_name), autocommit=True) as conn:
            conn.execute("CREATE TABLE tb_kept (id int)")
        try:
            yield url, role, password, db_name
        finally:
            _drop(admin, db_name)
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(f"DROP ROLE IF EXISTS {role}")  # ty: ignore[no-matching-overload]

    def test_the_rebuild_refuses_before_dropping_anything(self, role, tmp_path):
        url, role_name, password, db_name = role
        database_url = url.format(db_name)
        config, migrations = _project(tmp_path, database_url)

        with pytest.raises(DatabaseError) as info:
            RebuildStrategy(admin_url=url.format("postgres")).execute(
                config, migrations_dir=migrations, database_url=database_url
            )

        message = str(info.value)
        assert role_name in message
        assert "CREATEDB" in message
        assert password not in message
        with psycopg.connect(database_url) as conn:
            row = conn.execute("SELECT to_regclass('tb_kept')").fetchone()
        assert row
        assert row[0]

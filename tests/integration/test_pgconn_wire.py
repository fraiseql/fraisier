"""A connection URL reaches libpq as libpq would read it (#493).

Against a server that checks passwords: a password needing every escape a URL
has (``@``, ``+``, ``%``) connects whether it sits in the userinfo or in
``?password=``, through ``_pg_cmd`` and through a real restore. The control —
the same URL with a wrong password — is what makes the rest mean anything: a
server that trusts the connection accepts whatever fraisier sends.
"""

from __future__ import annotations

import getpass
import os
import subprocess
import uuid
from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest

from fraisier.dbops._pgconn import PgConn
from fraisier.dbops.operations import _pg_cmd

from .conftest import PW_PROBE_PASSWORD, PW_PROBE_ROLE

if TYPE_CHECKING:
    from pathlib import Path

    from .conftest import PgTarget

_WHOAMI = ["psql", "-XtA", "-c", "SELECT current_user"]


def _probe_url(
    server: PgTarget,
    db_name: str,
    *,
    password: str = PW_PROBE_PASSWORD,
    in_query: bool = False,
) -> str:
    """A URL for :data:`PW_PROBE_ROLE`, the password percent-encoded."""
    secret = quote(password, safe="")
    hostport = f"{server.host}:{server.port}"
    if in_query:
        return f"postgresql://{PW_PROBE_ROLE}@{hostport}/{db_name}?password={secret}"
    return f"postgresql://{PW_PROBE_ROLE}:{secret}@{hostport}/{db_name}"


def _connected_as(url: str) -> str:
    code, out, err = _pg_cmd(_WHOAMI, connection_url=url)
    assert code == 0, err
    return out.strip()


class TestAPasswordNeedingEveryEscape:
    def test_wrong_password_is_refused(self, password_pg):
        """The control. If this connects, the server is not checking."""
        url = _probe_url(password_pg, "postgres", password="not-the-password")

        code, _, err = _pg_cmd(_WHOAMI, connection_url=url)

        if code == 0:
            trusted = (
                "the server does not check passwords (pg_hba trust), so no "
                "test here can tell a right password from a mangled one"
            )
            pytest.fail(trusted)  # ty: ignore[invalid-argument-type]
        assert "password authentication failed" in err

    def test_in_the_userinfo(self, password_pg):
        assert _connected_as(_probe_url(password_pg, "postgres")) == PW_PROBE_ROLE

    def test_in_the_query(self, password_pg):
        url = _probe_url(password_pg, "postgres", in_query=True)

        assert _connected_as(url) == PW_PROBE_ROLE

    def test_through_a_restore(self, password_pg, tmp_path: Path):
        psycopg = pytest.importorskip("psycopg")
        from fraisier.dbops.restore import restore_backup

        suffix = uuid.uuid4().hex[:12]
        source, target = f"pw_src_{suffix}", f"pw_dst_{suffix}"
        admin = password_pg.dsn("postgres")
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f"CREATE DATABASE {source}")
            conn.execute(f"CREATE DATABASE {target} OWNER {PW_PROBE_ROLE}")
        try:
            with psycopg.connect(password_pg.dsn(source), autocommit=True) as conn:
                conn.execute("CREATE TABLE orders (id int)")
                conn.execute("INSERT INTO orders VALUES (1), (2)")
            dump = tmp_path / "src.dump"
            subprocess.run(
                ["pg_dump", "-Fc", "-f", str(dump)],
                env=PgConn.parse(password_pg.dsn(source)).child_env(),
                check=True,
            )

            result = restore_backup(
                backup_path=str(dump),
                db_name=target,
                connection_url=_probe_url(password_pg, "postgres"),
            )

            assert result.success, result.error
            with psycopg.connect(password_pg.dsn(target)) as conn:
                row = conn.execute("SELECT count(*) FROM orders").fetchone()
            assert row == (2,)
        finally:
            with psycopg.connect(admin, autocommit=True) as conn:
                for db in (source, target):
                    conn.execute(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)")


class TestAnAmbientServiceFile:
    """A service file outranks every ``PG*`` variable, so ``PGSERVICE`` in
    fraisier's environment would send a child to the service's server. It is
    refused before a child starts."""

    @pytest.fixture
    def ambient_service(self, tmp_path: Path, monkeypatch) -> None:
        service_file = tmp_path / "pg_service.conf"
        service_file.write_text("[acme]\nhost=/nonexistent_svc\n")
        monkeypatch.setenv("PGSERVICEFILE", str(service_file))
        monkeypatch.setenv("PGSERVICE", "acme")

    @pytest.mark.usefixtures("ambient_service")
    def test_is_refused_before_a_child_starts(self, password_pg):
        with pytest.raises(ValueError, match="PGSERVICE is set in the environment"):
            _pg_cmd(_WHOAMI, connection_url=_probe_url(password_pg, "postgres"))

    @pytest.mark.usefixtures("ambient_service")
    def test_would_redirect_a_child_that_kept_it(self, password_pg):
        """The control: with the URL as ``PG*`` and the service kept, libpq
        goes where the service file says."""
        conn = PgConn.parse(_probe_url(password_pg, "postgres"))

        proc = subprocess.run(
            _WHOAMI,
            env={**os.environ, **conn.pg_env()},
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode != 0
        assert "/nonexistent_svc" in proc.stderr


class TestAUserlessSocketUrl:
    """A URL without a user connects as the OS account, never ``PGUSER``."""

    @pytest.fixture
    def socket_url(self, pg_target) -> str:
        if not pg_target.over_socket:
            over_tcp = "the test server is reached over TCP, not a unix socket"
            pytest.skip(over_tcp)  # ty: ignore[too-many-positional-arguments]
        return pg_target.dsn("postgres")

    def test_connects(self, socket_url):
        code, out, err = _pg_cmd(
            ["psql", "-XtA", "-c", "SELECT 1"], connection_url=socket_url
        )

        assert (code, out.strip()) == (0, "1"), err

    def test_an_ambient_pguser_does_not_change_the_user(self, socket_url, monkeypatch):
        monkeypatch.setenv("PGUSER", "acme_nobody")

        assert _connected_as(socket_url) == getpass.getuser()

"""An in-process connection gets the session a child gets (#496).

libpq fills every keyword a conninfo leaves out from ``os.environ``; a child
started with ``PgConn.child_env()`` never sees those variables. Each test
below sets one, shows that a bare ``psycopg.connect(url)`` follows it (the
control: without it the pin proves nothing), and that a connection made with
``PgConn.conninfo()`` does not.
"""

from __future__ import annotations

import getpass
from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest

from fraisier.dbops._pgconn import PgConn

from .conftest import PW_PROBE_PASSWORD, PW_PROBE_ROLE

if TYPE_CHECKING:
    from pathlib import Path

psycopg = pytest.importorskip("psycopg")

_SQL = (
    "SELECT current_user, current_setting('search_path'), "
    "current_setting('client_encoding'), current_setting('application_name')"
)


def _bare(url: str) -> tuple[str, ...]:
    with psycopg.connect(url, connect_timeout=5) as conn:
        return tuple(conn.execute(_SQL).fetchone())


def _pinned(url: str) -> tuple[str, ...]:
    with psycopg.connect(PgConn.parse(url).conninfo(), connect_timeout=5) as conn:
        return tuple(conn.execute(_SQL).fetchone())


@pytest.fixture
def url(pg_target) -> str:
    return pg_target.dsn("postgres")


@pytest.mark.parametrize(
    ("var", "value", "column"),
    [
        ("PGOPTIONS", "-c search_path=acme_ambient", 1),
        ("PGCLIENTENCODING", "LATIN1", 2),
    ],
)
def test_an_ambient_session_setting_does_not_reach_it(
    url, monkeypatch, var, value, column
):
    clean = _pinned(url)
    monkeypatch.setenv(var, value)

    assert _bare(url)[column] != clean[column]
    assert _pinned(url) == clean


def test_an_ambient_target_session_attrs_does_not_reach_it(url, monkeypatch):
    """``standby`` refuses a primary: the bare connect fails, the pinned one
    connects."""
    monkeypatch.setenv("PGTARGETSESSIONATTRS", "standby")

    with pytest.raises(psycopg.OperationalError):
        _bare(url)
    assert _pinned(url)


def test_an_operator_default_still_reaches_it(url, monkeypatch):
    monkeypatch.setenv("PGAPPNAME", "acme_operator")

    assert _pinned(url)[3] == "acme_operator"


def test_an_ambient_service_is_refused(url, tmp_path: Path, monkeypatch):
    service_file = tmp_path / "pg_service.conf"
    service_file.write_text("[acme]\noptions=-c search_path=acme_service\n")
    monkeypatch.setenv("PGSERVICEFILE", str(service_file))
    monkeypatch.setenv("PGSERVICE", "acme")

    assert _bare(url)[1] == "acme_service"
    with pytest.raises(ValueError, match="PGSERVICE is set in the environment"):
        PgConn.parse(url).conninfo()


@pytest.mark.parametrize(
    ("var", "value", "setting", "pinned"),
    [
        ("PGTZ", "Asia/Tokyo", "TimeZone", "UTC"),
        ("PGDATESTYLE", "SQL, DMY", "DateStyle", "ISO"),
        ("PGGEQO", "off", "geqo", "on"),
    ],
)
def test_an_ambient_startup_setting_is_refused(
    url, monkeypatch, var, value, setting, pinned
):
    """No keyword can pin these, and an ambient one beats ``options``."""
    monkeypatch.setenv(var, value)
    options = PgConn.parse(url).params.get("options", "")
    with psycopg.connect(
        url, options=f"{options} -c {setting}={pinned}", connect_timeout=5
    ) as conn:
        reached = conn.execute(f"SHOW {setting}").fetchone()[0]

    assert reached == value
    with pytest.raises(ValueError, match=f"{var} is set in the environment"):
        PgConn.parse(url).conninfo()


class TestAUserlessSocketUrl:
    """The documented ``admin_url`` form names no user: it connects as the
    OS account, as its children do, never as ``PGUSER``."""

    @pytest.fixture
    def socket_url(self, pg_target) -> str:
        if not pg_target.over_socket:
            over_tcp = "the test server is reached over TCP, not a unix socket"
            pytest.skip(over_tcp)  # ty: ignore[too-many-positional-arguments]
        return pg_target.dsn("postgres")

    def test_an_ambient_pguser_does_not_change_the_user(self, socket_url, monkeypatch):
        monkeypatch.setenv("PGUSER", "acme_nobody")

        with pytest.raises(psycopg.OperationalError, match="acme_nobody"):
            _bare(socket_url)
        assert _pinned(socket_url)[0] == getpass.getuser()

    def test_an_ambient_pgport_does_not_change_the_server(
        self, socket_url, monkeypatch
    ):
        monkeypatch.setenv("PGPORT", "1")

        with pytest.raises(psycopg.OperationalError, match=r"\.s\.PGSQL\.1"):
            _bare(socket_url)
        assert _pinned(socket_url)[0] == getpass.getuser()


class TestAPasswordlessUrl:
    """Against a server that checks passwords: an ambient ``PGPASSWORD`` is
    not sent, while ``PGPASSFILE``, an operator default, is read."""

    @pytest.fixture
    def passwordless(self, password_pg) -> str:
        hostport = f"{password_pg.host}:{password_pg.port}"
        return f"postgresql://{PW_PROBE_ROLE}@{hostport}/postgres"

    def test_an_ambient_pgpassword_is_not_sent(self, passwordless, monkeypatch):
        monkeypatch.setenv("PGPASSWORD", PW_PROBE_PASSWORD)

        assert _bare(passwordless)[0] == PW_PROBE_ROLE
        with pytest.raises(psycopg.OperationalError, match="password"):
            _pinned(passwordless)

    def test_a_password_file_is_read(
        self, passwordless, password_pg, tmp_path: Path, monkeypatch
    ):
        passfile = tmp_path / "pgpass"
        passfile.write_text(
            f"{password_pg.host}:{password_pg.port}:*:{PW_PROBE_ROLE}:"
            f"{PW_PROBE_PASSWORD.replace('\\', '\\\\').replace(':', '\\:')}\n"
        )
        passfile.chmod(0o600)
        monkeypatch.setenv("PGPASSFILE", str(passfile))

        assert _pinned(passwordless)[0] == PW_PROBE_ROLE

    def test_the_urls_password_is_sent(self, passwordless):
        secret = quote(PW_PROBE_PASSWORD, safe="")
        url = passwordless.replace(f"{PW_PROBE_ROLE}@", f"{PW_PROBE_ROLE}:{secret}@")

        assert _pinned(url)[0] == PW_PROBE_ROLE

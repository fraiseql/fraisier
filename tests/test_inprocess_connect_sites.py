"""Every in-process connection fraisier makes goes through ``PgConn`` (#496).

Each site hands psycopg the URL as ``PgConn.conninfo()``: decoded, with
every keyword a child would not inherit pinned, so the session is the one a
child gets (``tests/integration/test_inprocess_connect.py`` shows it live). An
ambient ``PGSERVICE`` is refused before anything connects.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, patch

import psycopg
import pytest
from click.testing import CliRunner
from psycopg.conninfo import conninfo_to_dict

from fraisier.dbops._pgconn import PgConn

if TYPE_CHECKING:
    from collections.abc import Callable

URL = "postgresql://app:p%40w@db.acme.test:6543/acme_db?application_name=acme"


class _Stop(Exception):
    """Raised by the spy once the connection's arguments are recorded."""


def _pgbackrest(url: str) -> Any:
    from fraisier.dbops import pgbackrest

    return pgbackrest.database_exists(url, "acme_db")


def _tviews(url: str) -> Any:
    from fraisier.dbops import tviews

    return tviews.tviews_installed(url)


def _config(url: str) -> Any:
    envs = {"prod": {"database": {"database_url": url}}}
    return SimpleNamespace(fraises={"api": {"environments": envs}})


def _doctor(url: str) -> Any:
    from fraisier import doctor

    return doctor._check_pg_tviews_contract(_config(url))


def _adapter(url: str) -> Any:
    from fraisier.db.postgres_adapter import PostgresAdapter

    return asyncio.run(PostgresAdapter(url).connect())


def _test_database(url: str) -> Any:
    from fraisier.cli.test_components import test_database

    config = Mock()
    config.get_fraise_environment.return_value = {"type": "api"}
    deployer = Mock(database_config={"database_url": url})
    with (
        patch("fraisier.cli.test_components.require_config", return_value=config),
        patch("fraisier.cli.test_components._get_deployer", return_value=deployer),
    ):
        return CliRunner().invoke(test_database, ["api", "prod"]).output


#: site → (what it calls to connect, the call)
SITES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "pgbackrest": ("psycopg.connect", _pgbackrest),
    "tviews": ("psycopg.connect", _tviews),
    "doctor pg_tviews_contract": ("psycopg.connect", _doctor),
    "history db pool": ("fraisier.db.postgres_adapter.AsyncConnectionPool", _adapter),
    "test-database": ("psycopg.connect", _test_database),
}


def _spy(calls: list[tuple[tuple, dict]]) -> Callable[..., Any]:
    def record(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        raise _Stop

    return record


def _connect(site: str, url: str) -> tuple[Any, list[tuple[tuple, dict]]]:
    target, call = SITES[site]
    calls: list[tuple[tuple, dict]] = []
    outcome: Any = None
    with patch(target, side_effect=_spy(calls)):
        try:
            outcome = call(url)
        except (_Stop, ConnectionError, ValueError) as exc:
            outcome = exc
    return outcome, calls


def _conninfo(args: tuple, kwargs: dict) -> dict[str, Any]:
    """The libpq keywords a connection was given, wherever the site put them.

    A pool takes its connection keywords in ``kwargs=``; ``psycopg.connect``
    takes them beside the conninfo, and they override it.
    """
    given = conninfo_to_dict(kwargs.get("conninfo", *args[:1] or ("",)))
    extra = kwargs.get("kwargs", {}) if "min_size" in kwargs else kwargs
    return {**given, **{k: v for k, v in extra.items() if k not in _NOT_LIBPQ}}


#: Arguments of psycopg's own, not libpq keywords.
_NOT_LIBPQ = frozenset({"autocommit", "row_factory", "min_size", "max_size"})


@pytest.mark.parametrize("site", SITES)
class TestEachInProcessSite:
    def test_connects_with_pgconns_keywords(self, site):
        _, calls = _connect(site, URL)

        assert len(calls) == 1
        given = _conninfo(*calls[0])
        expected = PgConn.parse(URL).connect_kwargs()
        given.pop("connect_timeout", None)  # a site's own, over the URL's
        assert given == expected

    def test_an_ambient_pguser_changes_nothing(self, site, monkeypatch):
        monkeypatch.setenv("PGUSER", "acme_nobody")
        _, calls = _connect(site, "postgresql:///acme_db")

        assert _conninfo(*calls[0])["user"] == ""

    def test_an_ambient_service_is_refused_before_connecting(self, site, monkeypatch):
        monkeypatch.setenv("PGSERVICE", "acme")

        outcome, calls = _connect(site, URL)

        assert calls == []
        with contextlib.suppress(AttributeError):
            outcome = outcome.detail  # doctor's CheckResult
        assert "PGSERVICE is set in the environment" in str(outcome)


def test_test_database_shows_no_password():
    """The URL's password may sit in the query, after the ``@``."""
    secret = "acme-s3cr3t"
    url = f"postgresql://app@db.acme.test/acme_db?password={secret}"

    output, _ = _connect("test-database", url)

    assert secret not in output
    assert "app@db.acme.test/acme_db" in output


def test_test_database_reports_a_successful_connection():
    conn = Mock()
    conn.__enter__ = Mock(return_value=conn)
    conn.__exit__ = Mock(return_value=None)
    conn.execute.return_value.fetchone.return_value = (1,)
    with patch.object(psycopg, "connect", return_value=conn):
        output = _test_database("postgresql:///acme_db")

    assert "Database connection successful" in output

"""``pg_tviews_contract``: confiture 1.29 refuses a pg_tviews older than read contract 1.

The drift gate is on by default, so on a host whose pg_tviews predates
0.1.0-beta.20 every deploy of a TVIEW project fails ``CONFIG_014`` — after the
migrations ran.  ``pg_extension.extversion`` reads ``0.1.0`` on every beta, so the
check asks ``tviews.contract_version()`` instead.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import psycopg

from fraisier import doctor

if TYPE_CHECKING:
    import pytest

URL = "postgresql://app@db.example/app"


class _FakeConn:
    """Stands in for a psycopg connection; answers from a script."""

    def __init__(self, *, extension: bool, contract: object) -> None:
        self._extension = extension
        self._contract = contract
        self.queries: list[str] = []

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str) -> Any:
        self.queries.append(sql)
        if "pg_extension" in sql:
            rows = [(1,)] if self._extension else []
        elif "contract_version" in sql:
            if isinstance(self._contract, Exception):
                raise self._contract
            rows = [(self._contract,)]
        else:  # pragma: no cover - a query the check should not make
            raise AssertionError(sql)
        return SimpleNamespace(fetchone=lambda: rows[0] if rows else None)


def _config(*urls: str | None) -> Any:
    envs = {
        f"env{i}": {"database": {"database_url": url}} if url else {"database": {}}
        for i, url in enumerate(urls)
    }
    return SimpleNamespace(fraises={"api": {"environments": envs}})


def _run(
    monkeypatch: pytest.MonkeyPatch, conn: _FakeConn | Exception, *urls: str | None
):
    def connect(*_args: object, **_kwargs: object) -> _FakeConn:
        if isinstance(conn, Exception):
            raise conn
        return conn

    monkeypatch.setattr(psycopg, "connect", connect)
    return doctor._check_pg_tviews_contract(_config(*urls))


def test_registered_as_a_network_check() -> None:
    assert doctor.DOCTOR_CHECKS["pg_tviews_contract"].network is True


def test_skips_when_the_extension_is_absent(monkeypatch) -> None:
    result = _run(monkeypatch, _FakeConn(extension=False, contract=None), URL)

    assert result.status == "skip"


def test_fails_when_contract_version_is_missing(monkeypatch) -> None:
    missing = psycopg.errors.InvalidSchemaName('schema "tviews" does not exist')
    result = _run(monkeypatch, _FakeConn(extension=True, contract=missing), URL)

    assert result.status == "fail"
    assert "0.1.0-beta.20" in result.detail
    assert "migrate-from-0.1.0.sql" in (result.fix_hint or "")


def test_fails_when_the_contract_is_not_one(monkeypatch) -> None:
    result = _run(monkeypatch, _FakeConn(extension=True, contract=0), URL)

    assert result.status == "fail"


def test_passes_on_contract_one(monkeypatch) -> None:
    result = _run(monkeypatch, _FakeConn(extension=True, contract=1), URL)

    assert result.status == "pass"


def test_an_unreachable_database_is_a_skip_not_a_verdict(monkeypatch) -> None:
    result = _run(monkeypatch, psycopg.OperationalError("refused"), URL)

    assert result.status == "skip"
    assert "refused" in result.detail


def test_skips_without_a_config_or_a_database_url(monkeypatch) -> None:
    assert doctor._check_pg_tviews_contract(None).status == "skip"
    assert (
        _run(monkeypatch, _FakeConn(extension=True, contract=1), None).status == "skip"
    )

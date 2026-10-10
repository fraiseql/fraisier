"""``pg_tviews_contract``: confiture refuses a pg_tviews older than ``MINIMUM_PG_TVIEWS``.

The drift gate is on by default, so on a host whose pg_tviews confiture cannot
read every deploy of a TVIEW project fails ``CONFIG_014`` — after the
migrations ran.  ``pg_extension.extversion`` reads ``0.1.0`` on every beta, so the
check asks confiture's own ``require_supported_pg_tviews_on`` instead.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import confiture.platform
import psycopg
from confiture.exceptions import ConfigurationError

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

    def execute(self, query: Any) -> Any:
        sql = query if isinstance(query, str) else query.as_string()
        self.queries.append(sql)
        if "pg_extension" in sql:
            rows = [("tviews",)] if self._extension else []
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


_REFUSAL = ConfigurationError(
    "pg_tviews 0.1.0's tviews.registry has no function_reads, time_refresh; "
    "confiture reads the registry of pg_tviews 0.1.0-beta.26 and later.",
    error_code="CONFIG_014",
    resolution_hint=(
        "Upgrade the server's pg_tviews to 0.1.0-beta.26 or later, then "
        "ALTER EXTENSION pg_tviews UPDATE"
    ),
)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    conn: _FakeConn | Exception,
    *urls: str | None,
    refusal: Exception | None = None,
):
    """Run the check with confiture's verdict scripted: *refusal* raised, or a pass.

    Returns the result and what confiture was asked about.
    """

    def connect(*_args: object, **_kwargs: object) -> _FakeConn:
        if isinstance(conn, Exception):
            raise conn
        return conn

    def require(database: object) -> None:
        asked.append(database)
        if refusal is not None:
            raise refusal

    asked: list[object] = []
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(confiture.platform, "require_supported_pg_tviews_on", require)
    return doctor._check_pg_tviews_contract(_config(*urls)), asked


def test_registered_as_a_network_check() -> None:
    assert doctor.DOCTOR_CHECKS["pg_tviews_contract"].network is True


def test_skips_when_the_extension_is_absent(monkeypatch) -> None:
    result, _asked = _run(monkeypatch, _FakeConn(extension=False, contract=None), URL)

    assert result.status == "skip"


def test_fails_when_confiture_refuses_the_pg_tviews(monkeypatch) -> None:
    result, _asked = _run(
        monkeypatch, _FakeConn(extension=True, contract=1), URL, refusal=_REFUSAL
    )

    assert result.status == "fail"
    assert "function_reads, time_refresh" in result.detail
    assert "0.1.0-beta.26" in result.detail
    assert "ALTER EXTENSION pg_tviews UPDATE" in (result.fix_hint or "")


def test_asks_confiture_on_its_own_connection_not_the_url(monkeypatch) -> None:
    """A URL confiture cannot connect to is ``CONFIG_006``; doctor's is a skip."""
    conn = _FakeConn(extension=True, contract=1)
    _result, asked = _run(monkeypatch, conn, URL)

    assert asked == [conn]


def test_passes_when_confiture_reads_the_pg_tviews(monkeypatch) -> None:
    result, _asked = _run(monkeypatch, _FakeConn(extension=True, contract=1), URL)

    assert result.status == "pass"
    assert "0.1.0-beta.26" in result.detail


def test_an_unreachable_database_is_a_skip_not_a_verdict(monkeypatch) -> None:
    result, _asked = _run(monkeypatch, psycopg.OperationalError("refused"), URL)

    assert result.status == "skip"
    assert "refused" in result.detail


def test_skips_without_a_config_or_a_database_url(monkeypatch) -> None:
    assert doctor._check_pg_tviews_contract(None).status == "skip"
    assert (
        _run(monkeypatch, _FakeConn(extension=True, contract=1), None)[0].status
        == "skip"
    )

"""The probe reports the identity a child gets, on a real server.

An ambient ``PGUSER`` or service file used to be able to change who a child
connected as without the URL saying so. The probe connects as the children
do, so neither changes its answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest

from fraisier.dbops.identity import probe_identity

from .conftest import PW_PROBE_PASSWORD, PW_PROBE_ROLE

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def probe_url(password_pg) -> str:
    secret = quote(PW_PROBE_PASSWORD, safe="")
    hostport = f"{password_pg.host}:{password_pg.port}"
    return f"postgresql://{PW_PROBE_ROLE}:{secret}@{hostport}/postgres"


def test_reports_the_user_and_database_the_server_saw(probe_url):
    found = probe_identity(probe_url)

    assert (found.user, found.database, found.error) == (
        PW_PROBE_ROLE,
        "postgres",
        None,
    )
    assert found.host


def test_an_ambient_pguser_changes_nothing(probe_url, monkeypatch):
    monkeypatch.setenv("PGUSER", "postgres")

    found = probe_identity(probe_url)

    assert (found.user, found.database) == (PW_PROBE_ROLE, "postgres")


def test_an_ambient_service_is_reported_not_followed(
    probe_url, tmp_path: Path, monkeypatch
):
    service_file = tmp_path / "pg_service.conf"
    service_file.write_text("[acme]\nuser=postgres\ndbname=acme_elsewhere\n")
    monkeypatch.setenv("PGSERVICEFILE", str(service_file))
    monkeypatch.setenv("PGSERVICE", "acme")

    found = probe_identity(probe_url)

    assert found.user is None
    assert "PGSERVICE is set in the environment" in (found.error or "")


def test_a_wrong_password_is_reported_not_raised(password_pg):
    hostport = f"{password_pg.host}:{password_pg.port}"
    found = probe_identity(f"postgresql://{PW_PROBE_ROLE}:nope@{hostport}/postgres")

    assert found.user is None
    assert "password authentication failed" in (found.error or "")

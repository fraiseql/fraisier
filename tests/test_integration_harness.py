"""The integration harness must find the server CI actually provides (#358).

Nine tests proving a truncated dump fails the restore, and nine proving the
actuation receipt round-trips through real SQL, reported green for a release
without ever running: the harness discovered PostgreSQL over a unix socket
only, and every CI workflow provides it as a service container over TCP with no
shared socket. The fixture skipped, pytest counted a skip as not-a-failure, and
the checkmark said nothing about the behaviour being shipped.

These are unit tests over the discovery itself — they need no database — so the
harness's own reachability logic is pinned by tests that always run.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import PgTarget, _discover_target


class TestPgTargetDsn:
    """One target, two transports, one DSN builder."""

    def test_a_socket_target_addresses_the_directory(self):
        target = PgTarget(host="/run/postgresql")

        assert target.over_socket is True
        assert (
            target.dsn("fraisier_it")
            == "postgresql:///fraisier_it?host=/run/postgresql"
        )

    def test_a_tcp_target_carries_host_port_and_credentials(self):
        """CI's service container, exactly as the workflows declare it."""
        target = PgTarget(
            host="localhost", port=5432, user="fraisier", password="fraisier"
        )

        assert target.over_socket is False
        assert (
            target.dsn("fraisier_it")
            == "postgresql://fraisier:fraisier@localhost:5432/fraisier_it"
        )

    def test_credentials_are_percent_encoded(self):
        """A password is data, not URL syntax."""
        target = PgTarget(
            host="db.internal", port=5432, user="a b", password="p@ss/w:d"
        )

        assert target.dsn("x") == "postgresql://a%20b:p%40ss%2Fw%3Ad@db.internal:5432/x"

    def test_a_tcp_target_without_credentials_omits_the_userinfo(self):
        target = PgTarget(host="localhost", port=5432)

        assert target.dsn("x") == "postgresql://localhost:5432/x"


class TestDiscovery:
    """Which server the fixtures hand the tests."""

    def test_the_ci_service_container_is_discovered(self, monkeypatch):
        """FRAISIER_TEST_PG_URL is a candidate, so CI's TCP server is found.

        This is the whole bug: without it ``_discover_target`` returns None on
        every CI runner and the tests carrying #358's guarantee skip.
        """
        monkeypatch.setenv(
            "FRAISIER_TEST_PG_URL",
            "postgresql://fraisier:fraisier@localhost:5432/fraisier_test",
        )
        monkeypatch.setattr(
            "tests.integration.conftest._probe", lambda _dsn: "/var/run/postgresql"
        )
        monkeypatch.setattr(
            "tests.integration.conftest.shutil.which", lambda tool: tool
        )

        target = _discover_target()

        assert target is not None
        assert target == PgTarget(
            host="localhost", port=5432, user="fraisier", password="fraisier"
        )
        # Addressed over TCP even though the probe reported a socket directory:
        # the server's own socket is inside the service container and is not
        # shared with the runner.
        assert target.dsn("postgres").startswith("postgresql://fraisier:")

    def test_an_unusable_env_url_falls_back_to_the_local_socket(self, monkeypatch):
        """A stale FRAISIER_TEST_PG_URL must not hide a working local server."""
        monkeypatch.setenv("FRAISIER_TEST_PG_URL", "postgresql://nobody@127.0.0.1:1/db")
        monkeypatch.setattr(
            "tests.integration.conftest.shutil.which", lambda tool: tool
        )
        monkeypatch.setattr(
            "tests.integration.conftest._probe",
            lambda dsn: None if "127.0.0.1" in dsn else "/run/postgresql",
        )

        assert _discover_target() == PgTarget(host="/run/postgresql")

    def test_no_env_url_still_discovers_a_socket(self, monkeypatch):
        """The developer's path is unchanged."""
        monkeypatch.delenv("FRAISIER_TEST_PG_URL", raising=False)
        monkeypatch.setattr(
            "tests.integration.conftest.shutil.which", lambda tool: tool
        )
        monkeypatch.setattr(
            "tests.integration.conftest._probe", lambda _dsn: "/run/postgresql"
        )

        assert _discover_target() == PgTarget(host="/run/postgresql")

    def test_missing_client_tools_are_not_a_reachable_server(self, monkeypatch):
        monkeypatch.setenv("FRAISIER_TEST_PG_URL", "postgresql://f:f@localhost:5432/d")
        monkeypatch.setattr(
            "tests.integration.conftest.shutil.which", lambda _tool: None
        )

        assert _discover_target() is None

    @pytest.mark.parametrize("url", ["", "not-a-url", "postgresql:///db"])
    def test_an_env_url_naming_no_host_is_no_candidate(self, url, monkeypatch):
        """Only a URL with a host describes a server reachable over TCP."""
        monkeypatch.setenv("FRAISIER_TEST_PG_URL", url)
        monkeypatch.setattr(
            "tests.integration.conftest.shutil.which", lambda tool: tool
        )
        monkeypatch.setattr(
            "tests.integration.conftest._probe",
            lambda dsn: (
                "/run/postgresql" if "host=" in dsn or dsn.count("/") == 3 else None
            ),
        )

        assert _discover_target() == PgTarget(host="/run/postgresql")

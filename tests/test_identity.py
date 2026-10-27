"""Who each deploy connection actually connects as.

The probe runs ``psql`` the way every other child runs it, through
``_pg_cmd``, so it reports the identity the children get rather than the one
the URL suggests: an ambient ``PGUSER`` or a service file cannot make the two
differ, because neither reaches a child.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from fraisier.dbops import identity
from fraisier.dbops.identity import ConnectedAs, note_connection, probe_identity

URL = "postgresql://acme:pw@db.acme.test:5432/acme_db"


def _psql(code: int, out: str = "", err: str = ""):
    return patch.object(identity, "_pg_cmd", return_value=(code, out, err))


class TestProbeIdentity:
    def test_reads_user_database_and_server_address(self):
        with _psql(0, "acme|acme_db|10.0.0.5\n"):
            assert probe_identity(URL) == ConnectedAs("acme", "acme_db", "10.0.0.5")

    def test_over_a_socket_names_the_socket_directory(self):
        with _psql(0, "acme|acme_db|\n"):
            found = probe_identity("postgresql:///acme_db?host=/run/postgresql")

        assert found.host == "/run/postgresql"

    def test_over_the_default_socket_says_so(self):
        with _psql(0, "acme|acme_db|\n"):
            assert probe_identity("postgresql:///acme_db").host == "local socket"

    def test_a_failed_connection_is_an_error_not_an_exception(self):
        err = 'psql: error: FATAL:  password authentication failed for user "acme"\n'
        with _psql(2, err=err):
            found = probe_identity(URL)

        assert found.user is None
        assert found.error == err.strip()

    def test_nothing_it_raises_escapes(self):
        with patch.object(identity, "_pg_cmd", side_effect=OSError("no psql")):
            assert probe_identity(URL).error == "no psql"

    def test_a_server_that_never_answers_is_an_error_in_bounded_time(self):
        import subprocess

        timeout = subprocess.TimeoutExpired(["psql"], identity.PROBE_TIMEOUT)
        with patch.object(identity, "_pg_cmd", side_effect=timeout) as pg_cmd:
            found = probe_identity(URL)

        assert pg_cmd.call_args.kwargs["timeout"] == identity.PROBE_TIMEOUT
        assert found.error == f"no answer within {identity.PROBE_TIMEOUT:g}s"

    def test_the_bound_reaches_the_child(self):
        import subprocess

        from fraisier.dbops import operations

        done = subprocess.CompletedProcess([], 0, "acme|acme_db|10.0.0.5\n", "")
        with patch.object(operations.subprocess, "run", return_value=done) as run:
            probe_identity(URL)

        assert run.call_args.kwargs["timeout"] == identity.PROBE_TIMEOUT

    def test_it_launches_like_every_other_child(self):
        with _psql(0, "acme|acme_db|10.0.0.5\n") as pg_cmd:
            probe_identity(URL)

        assert pg_cmd.call_args.kwargs["connection_url"] == URL


class TestOutsideADeploy:
    def test_noting_a_connection_probes_nothing(self):
        with _psql(0, "acme|acme_db|10.0.0.5\n") as pg_cmd:
            note_connection(URL, "app")

        pg_cmd.assert_not_called()
        assert identity.recorded() is None


class TestDuringADeploy:
    def test_each_distinct_url_is_probed_once(self):
        with identity.recording(), _psql(0, "acme|acme_db|10.0.0.5\n") as pg_cmd:
            note_connection(URL, "app")
            note_connection(URL, "app")

        assert pg_cmd.call_count == 1

    def test_identities_are_recorded_per_role(self):
        with identity.recording():
            with _psql(0, "acme|acme_db|10.0.0.5\n"):
                note_connection(URL, "app")
            with _psql(0, "postgres|postgres|10.0.0.5\n"):
                note_connection("postgresql://postgres@db.acme.test/postgres", "admin")
            recorded = identity.recorded()

        assert recorded == {
            "app": {"user": "acme", "database": "acme_db", "host": "10.0.0.5"},
            "admin": {"user": "postgres", "database": "postgres", "host": "10.0.0.5"},
        }

    def test_a_failed_probe_is_recorded_as_its_error(self):
        with identity.recording(), _psql(2, err="psql: error: refused\n"):
            note_connection(URL, "app")
            recorded = identity.recorded()

        assert recorded == {"app": {"error": "psql: error: refused"}}

    def test_each_identity_is_logged_at_info(self, caplog):
        caplog.set_level(logging.INFO, logger="fraisier")
        with identity.recording(), _psql(0, "acme|acme_db|10.0.0.5\n"):
            note_connection(URL, "app")

        assert "connected as acme to acme_db on 10.0.0.5 (role: app)" in caplog.text

    def test_a_second_role_on_the_same_url_is_recorded_too(self):
        """A single-role setup, or the preflight against the admin URL."""
        with identity.recording(), _psql(0, "acme|acme_db|10.0.0.5\n") as pg_cmd:
            note_connection(URL, "admin")
            note_connection(URL, "preflight")
            recorded = identity.recorded()

        assert pg_cmd.call_count == 1
        assert recorded is not None
        assert recorded["preflight"] == recorded["admin"]

    def test_a_role_keeps_the_first_url_noted_for_it(self):
        with identity.recording():
            with _psql(0, "acme|acme_db|10.0.0.5\n"):
                note_connection(URL, "admin")
            with _psql(0, "other|other_db|10.0.0.6\n"):
                note_connection("postgresql://other@db.acme.test/other_db", "admin")
            recorded = identity.recorded()

        assert recorded is not None
        assert recorded["admin"]["user"] == "acme"

    def test_the_log_never_carries_the_password(self, caplog):
        caplog.set_level(logging.DEBUG)
        with identity.recording(), _psql(2, err="psql: error: refused\n"):
            note_connection(URL, "app")

        assert ":pw@" not in caplog.text


class TestFirstUse:
    """A deploy URL is first used where it is resolved, at strategy entry."""

    @pytest.mark.parametrize(
        ("role", "label"), [("database_url", "app"), ("admin_url", "admin")]
    )
    def test_resolving_a_url_notes_it(self, role, label):
        from fraisier.dbops._url import resolve_db_url

        with patch.object(identity, "note_connection") as note:
            resolve_db_url(URL, role=role)

        note.assert_called_once_with(URL, label)

    def test_a_restore_notes_its_admin_url(self):
        from fraisier.strategies import _restore

        config = _restore.RestoreConfig(db_name="acme_db", backup_dir=Path())
        with patch.object(identity, "note_connection") as note:
            _restore.RestoreMigrateStrategy(config, admin_url=URL)

        note.assert_called_once_with(URL, "admin")

    def test_a_restore_names_the_field_it_refuses(self):
        from fraisier.strategies import _restore

        config = _restore.RestoreConfig(db_name="acme_db", backup_dir=Path())
        with pytest.raises(ValueError, match=r"^admin_url: "):
            _restore.RestoreMigrateStrategy(config, admin_url=URL + "?service=x")

    def test_the_preflight_target_notes_itself(self, tmp_path: Path):
        import subprocess

        from fraisier.dbops import preflight

        done = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}")
        with (
            patch.object(identity, "note_connection") as note,
            patch.object(preflight.subprocess, "run", return_value=done),
            pytest.raises(Exception),  # noqa: B017 - the payload is a stub
        ):
            preflight._run_confiture_preflight(tmp_path / "c.yaml", tmp_path, URL)

        note.assert_called_once_with(URL, "preflight")


class TestTheDeploymentRecord:
    def test_the_status_written_during_a_deploy_carries_the_identities(
        self, tmp_path: Path
    ):
        from fraisier.status import read_status
        from tests.test_status_file_truth import (
            FRAISE,
            _deployer,
            _git_and_service,
            _with_database,
        )

        deployer = _deployer(tmp_path, **_with_database())

        def migrate(*_a: Any, **_kw: Any) -> None:
            note_connection(URL, "app")

        with (
            _git_and_service(deployer),
            patch.object(deployer, "_run_database_migrations", side_effect=migrate),
            _psql(0, "acme|acme_db|10.0.0.5\n"),
        ):
            deployer.execute()

        status = read_status(FRAISE, status_dir=tmp_path / "status")
        assert status is not None
        assert status.connected_as == {
            "app": {"user": "acme", "database": "acme_db", "host": "10.0.0.5"}
        }

    def test_an_old_record_has_none(self):
        from fraisier.status import _from_dict

        status = _from_dict({"fraise_name": "a", "environment": "b"})

        assert status.connected_as is None


class TestStatusShowsTheRecord:
    """``fraisier status`` reads the identities the last deploy recorded; it
    never connects to find them."""

    @staticmethod
    def _status(connected_as):
        from fraisier.status import DeploymentStatusFile

        return DeploymentStatusFile("a", "b", connected_as=connected_as)

    def test_one_line_per_role(self):
        from fraisier.cli._info import _connected_as_lines

        lines = _connected_as_lines(
            self._status(
                {
                    "app": {"user": "acme", "database": "acme_db", "host": "h"},
                    "admin": {"error": "psql: error: refused"},
                }
            ),
            "b",
        )

        assert lines == [
            "[bold]Connected As:[/bold] (last deploy)",
            "  app: acme to acme_db on h",
            "  admin: [red]unknown[/red] (psql: error: refused)",
        ]

    @pytest.mark.parametrize("status", [None, "no record"])
    def test_nothing_recorded_prints_nothing(self, status):
        from fraisier.cli._info import _connected_as_lines

        record = None if status is None else self._status(None)

        assert _connected_as_lines(record, "b") == []

    def test_another_environments_record_prints_nothing(self):
        """The status file is per fraise: its last deploy may be another env."""
        from fraisier.cli._info import _connected_as_lines

        record = self._status({"app": {"user": "u"}})

        assert _connected_as_lines(record, "production") == []

    def test_it_never_connects(self):
        from fraisier.cli._info import _connected_as_lines

        with patch.object(identity, "_pg_cmd", side_effect=AssertionError):
            _connected_as_lines(self._status({"app": {"user": "u"}}), "b")


class TestDoctorShowsEachRole:
    """``fraisier doctor`` connects, as a deploy would, once per role."""

    @staticmethod
    def _config(**database: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            fraises={"api": {"environments": {"production": {"database": database}}}}
        )

    @staticmethod
    def _run(config):
        from fraisier import doctor

        return doctor.DOCTOR_CHECKS["database_connected_as"].fn(config)

    def test_one_line_per_role(self):
        answers = {
            URL: ConnectedAs("acme", "acme_db", "h"),
            "postgresql://postgres@h/postgres": ConnectedAs(
                "postgres", "postgres", "h"
            ),
        }
        with patch.object(identity, "probe_identity", side_effect=answers.get):
            result = self._run(
                self._config(
                    database_url=URL, admin_url="postgresql://postgres@h/postgres"
                )
            )

        assert result.status == "pass"
        assert result.detail.splitlines() == [
            "api/production app: acme to acme_db on h, sslmode prefer (no certificate check)",
            "api/production admin: postgres to postgres on h, sslmode prefer (no certificate check)",
        ]

    def test_a_role_that_cannot_connect_warns_with_its_error(self):
        with patch.object(
            identity, "probe_identity", return_value=ConnectedAs(error="refused")
        ):
            result = self._run(self._config(database_url=URL))

        assert result.status == "warn"
        assert result.detail == (
            "api/production app: refused, sslmode prefer (no certificate check)"
        )

    def test_a_socket_url_shows_no_tls(self):
        url = "postgresql:///acme_db"
        with patch.object(
            identity, "probe_identity", return_value=ConnectedAs("acme", "acme_db", "s")
        ):
            result = self._run(self._config(database_url=url))

        assert (
            result.detail
            == "api/production app: acme to acme_db on s, sslmode none (socket)"
        )

    def test_an_ambient_policy_is_the_roles_error(self, monkeypatch):
        """The probe launches as a deploy child does, so it is refused the same way."""
        monkeypatch.setenv("PGSSLMODE", "verify-full")
        with patch.object(identity, "_pg_cmd", side_effect=AssertionError):
            result = self._run(self._config(database_url=URL))

        assert result.status == "warn"
        assert "PGSSLMODE" in result.detail

    def test_nothing_configured_is_a_skip(self):
        assert self._run(self._config()).status == "skip"

    def test_it_connects_so_it_is_a_network_check(self):
        from fraisier import doctor

        assert doctor.DOCTOR_CHECKS["database_connected_as"].network is True

    def test_a_url_a_deploy_would_refuse_is_the_roles_error(self):
        with patch.object(identity, "probe_identity") as probe:
            result = self._run(
                self._config(database_url="postgresql://h/db?service=acme")
            )

        probe.assert_not_called()
        assert result.status == "warn"
        assert result.detail.startswith("api/production app: database_url: ")
        assert "'service'" in result.detail

    def test_each_line_renders_under_the_first(self, capsys):
        from fraisier.cli import doctor as cli_doctor
        from fraisier.doctor import CheckResult

        cli_doctor.render_text(
            [CheckResult("database_connected_as", "pass", "a app: x\na admin: y")]
        )

        first, second = capsys.readouterr().out.splitlines()[:2]
        assert second.index("a admin") == first.index("a app")


class TestTransportPolicyAtTheFrontDoors:
    """``resolve_db_url``, ``fraisier validate`` and doctor ask PgConn's own
    check, so a deploy is refused before it does anything."""

    def test_resolving_refuses_naming_the_field(self, monkeypatch):
        from fraisier.dbops._url import resolve_db_url

        monkeypatch.setenv("PGSSLMODE", "verify-full")

        with pytest.raises(ValueError, match=r"^admin_url: PGSSLMODE"):
            resolve_db_url(URL, role="admin_url")

    def test_validate_reports_it(self, monkeypatch):
        from fraisier.config._validation import validate_pg_url_string

        monkeypatch.setenv("PGSSLMODE", "verify-full")

        (error,) = validate_pg_url_string("api", "database_url", URL)

        assert error.startswith("api: database.database_url: PGSSLMODE")
        assert ":pw@" not in error

"""A deploy says when ``migrate up`` updated pg_tviews, and when it could not.

From confiture 1.45.0, ``migrate up`` brings a database's pg_tviews catalog to the
library its server loaded before it applies anything: ``ALTER EXTENSION pg_tviews
UPDATE`` when the library is one release past the catalog, reported only through
the ``pg_tviews_updated`` event.  Every other case is ``CONFIG_016`` and changes
nothing: a catalog no update reaches, an update PostgreSQL refused, and a dry run
inside the window.

The refusals are built from the envelopes the published 1.45.0 emitted on a live
upgrade (``tests/fixtures/confiture/*config-016*``): the error's ``message``,
``code`` and ``actionable`` are what the in-process ``ConfigurationError`` carries
as its message, ``error_code`` and ``resolution_hint``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from confiture.core.migrator import UpEvent, UpObserver
from confiture.exceptions import ConfigurationError as ConfitureConfigurationError
from confiture.exceptions import MigrationError as ConfitureMigrationError
from confiture.exceptions import SQLError
from confiture.models.results import MigrateDownResult, MigrateUpResult

from fraisier.errors import DatabaseError, PgTviewsUpdateError
from fraisier.errors import MigrationError as FraisierMigrationError

_FIXTURES = Path(__file__).parent / "fixtures" / "confiture"
_CONFIG = Path("/srv/acme/db/environments/production.yaml")


def _refusal(fixture: str) -> ConfitureConfigurationError:
    """confiture's in-process refusal, as the envelope in *fixture* reports it."""
    error = json.loads((_FIXTURES / fixture).read_text())["error"]
    return ConfitureConfigurationError(
        error["message"],
        error_code=error["code"],
        resolution_hint=error["actionable"],
    )


def _no_update() -> ConfitureConfigurationError:
    return _refusal("migrate-up-config-016-no-update-beta35-beta37.json")


def _owner_reads() -> ConfitureConfigurationError:
    """PostgreSQL refused the update; confiture chains its error as the cause."""
    refusal = _refusal("migrate-up-config-016-owner-reads-beta36-beta37.json")
    primary = str(refusal).split("PostgreSQL refused it: ", 1)[1].split("\n", 1)[0]
    refusal.__cause__ = psycopg.errors.InsufficientPrivilege(primary)
    return refusal


def _dry_run() -> ConfitureConfigurationError:
    return _refusal("migrate-up-dry-run-config-016-beta36-beta37.json")


def _env() -> MagicMock:
    env = MagicMock()
    env.migration.view_helpers = "manual"
    return env


def _wire(migrator: MagicMock, session: MagicMock) -> None:
    ctx = migrator.from_config.return_value
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)


def _refusing(error: Exception) -> MagicMock:
    session = MagicMock()
    session.up.side_effect = error
    session.down.side_effect = error
    return session


def _applied() -> MigrateUpResult:
    return MigrateUpResult(
        success=True,
        migrations_applied=[],
        total_duration_ms=1,
        checksums_verified=True,
        dry_run=False,
    )


def _migrate_up(session: MagicMock, **kwargs: object) -> None:
    from fraisier.dbops.confiture import migrate_up

    with (
        patch("fraisier.dbops.confiture.Migrator") as migrator,
        patch("fraisier.dbops.confiture._load_env", return_value=_env()),
    ):
        _wire(migrator, session)
        migrate_up(_CONFIG, **kwargs)  # ty: ignore[invalid-argument-type]


class TestACatalogNoUpdateReaches:
    def test_is_its_own_failure_not_a_configuration_one(self) -> None:
        with pytest.raises(PgTviewsUpdateError) as excinfo:
            _migrate_up(_refusing(_no_update()))

        assert excinfo.value.code == "PG_TVIEWS_NOT_UPDATED"

    def test_is_a_database_failure_that_applied_nothing(self) -> None:
        """The deployer records ``steps_applied`` off a ``DatabaseError`` (#272)."""
        with pytest.raises(DatabaseError) as excinfo:
            _migrate_up(_refusing(_no_update()))

        assert excinfo.value.steps_applied == 0

    def test_names_both_releases(self) -> None:
        with pytest.raises(PgTviewsUpdateError) as excinfo:
            _migrate_up(_refusing(_no_update()))

        assert "0.1.0-beta.35" in str(excinfo.value)
        assert "0.1.0-beta.37" in str(excinfo.value)

    def test_names_the_recreate_to_run_by_hand_with_its_config(self) -> None:
        """confiture's hint omits ``--config``, which the recreate requires."""
        with pytest.raises(PgTviewsUpdateError) as excinfo:
            _migrate_up(_refusing(_no_update()))

        assert (
            f"confiture migrate up --recreate-pg-tviews --config {_CONFIG}"
            in excinfo.value.recovery_hint
        )


class TestAnUpdatePostgresqlRefused:
    def test_is_its_own_failure(self) -> None:
        with pytest.raises(PgTviewsUpdateError):
            _migrate_up(_refusing(_owner_reads()))

    def test_names_the_tview_the_role_and_the_privilege(self) -> None:
        with pytest.raises(PgTviewsUpdateError) as excinfo:
            _migrate_up(_refusing(_owner_reads()))

        assert "tv_tag (acme_owner lacks SELECT on public.tb_tag)" in str(excinfo.value)

    def test_tells_to_grant_the_owner_not_to_become_superuser(self) -> None:
        """confiture's hint for any refused statement is about extension owners."""
        with pytest.raises(PgTviewsUpdateError) as excinfo:
            _migrate_up(_refusing(_owner_reads()))

        hint = excinfo.value.recovery_hint
        assert "grant" in hint.lower()
        assert "superuser" not in hint

    def test_says_the_extension_kept_its_release(self) -> None:
        with pytest.raises(PgTviewsUpdateError) as excinfo:
            _migrate_up(_refusing(_owner_reads()))

        assert "still on pg_tviews 0.1.0-beta.36" in str(excinfo.value)


def _emitting(event: UpEvent) -> MagicMock:
    """A session whose ``up`` delivers *event* to the observer fraisier passed."""
    session = MagicMock()

    def up(*, on_event: UpObserver, **_: object) -> MigrateUpResult:
        on_event(event)
        return _applied()

    session.up.side_effect = up
    return session


class TestTheUpdateMigrateUpRan:
    def test_is_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        updated = UpEvent(
            "pg_tviews_updated",
            message="pg_tviews 0.1.0-beta.36 updated to 0.1.0-beta.37",
        )
        with caplog.at_level(logging.WARNING, logger="fraisier.dbops.confiture"):
            _migrate_up(_emitting(updated))

        assert "pg_tviews 0.1.0-beta.36 updated to 0.1.0-beta.37" in caplog.text
        assert "ALTER EXTENSION pg_tviews UPDATE" in caplog.text

    def test_other_events_are_not(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="fraisier.dbops.confiture"):
            _migrate_up(_emitting(UpEvent("lock_acquired")))

        assert "pg_tviews" not in caplog.text


class TestADryRunInsideTheWindow:
    def test_is_its_own_failure_not_a_failed_verification(self) -> None:
        """``pre_migrate_verify`` dry-runs first, and a dry run updates nothing."""
        with pytest.raises(PgTviewsUpdateError) as excinfo:
            _migrate_up(_refusing(_dry_run()), pre_migrate_verify=True)

        assert "pre_migrate_verify" in str(excinfo.value)

    def test_names_the_update_to_run_by_hand(self) -> None:
        with pytest.raises(PgTviewsUpdateError) as excinfo:
            _migrate_up(_refusing(_dry_run()), pre_migrate_verify=True)

        assert "ALTER EXTENSION pg_tviews UPDATE" in excinfo.value.recovery_hint


def test_another_configuration_refusal_is_left_alone() -> None:
    other = ConfitureConfigurationError("no", error_code="CONFIG_015")
    with pytest.raises(ConfitureConfigurationError):
        _migrate_up(_refusing(other))


def test_a_rollback_refused_the_same_way_is_reported_the_same_way() -> None:
    """``migrate down`` brings the catalog up to date first too (1.45.0)."""
    from fraisier.dbops.confiture import migrate_down

    with (
        patch("fraisier.dbops.confiture.Migrator") as migrator,
        patch("fraisier.dbops.confiture._resolve_config", return_value=_env()),
    ):
        _wire(migrator, _refusing(_no_update()))
        with pytest.raises(PgTviewsUpdateError):
            migrate_down(_CONFIG, steps=1)


def test_a_rollback_result_is_unchanged_when_nothing_is_refused() -> None:
    from fraisier.dbops.confiture import migrate_down

    session = MagicMock()
    session.down.return_value = MigrateDownResult(
        success=True, migrations_rolled_back=[], total_duration_ms=1
    )
    with (
        patch("fraisier.dbops.confiture.Migrator") as migrator,
        patch("fraisier.dbops.confiture._resolve_config", return_value=_env()),
    ):
        _wire(migrator, session)
        assert migrate_down(_CONFIG, steps=1).success


def test_postgresql_s_context_lines_do_not_bury_the_outcome() -> None:
    """The refused statement's HINT and CONTEXT follow its first line."""
    refusal = _owner_reads()
    refusal.args = (f"{refusal.args[0]}\nCONTEXT:  PL/pgSQL function x line 41",)
    with pytest.raises(PgTviewsUpdateError) as excinfo:
        _migrate_up(_refusing(refusal))

    assert "CONTEXT" not in excinfo.value.message
    assert excinfo.value.message.endswith("no migration was applied")


def _failed_on(pg_error: psycopg.Error) -> MigrateUpResult:
    """A migration PostgreSQL refused, chained as confiture 1.45.0 chains it.

    ``failure`` is confiture's ``MigrationError``, caused by its ``SQLError``,
    caused by psycopg's error (measured on a live refusal).
    """
    sql_error = SQLError("CREATE TABLE tv_profile AS ...", None, pg_error)
    sql_error.__cause__ = pg_error
    failure = ConfitureMigrationError(f"Failed to apply migration: {pg_error}")
    failure.__cause__ = sql_error
    return MigrateUpResult(
        success=False,
        migrations_applied=[],
        total_duration_ms=1,
        checksums_verified=True,
        dry_run=False,
        errors=[str(failure)],
        failure=failure,
    )


def _failing(result: MigrateUpResult) -> MagicMock:
    session = MagicMock()
    session.up.return_value = result
    return session


#: pg_tviews 0.1.0-beta.37's refusal of a definition that reads another role's
#: TVIEW, verbatim from a live run.
_TRIGGER_REFUSED = (
    'permission denied for table "public"."tv_user": this TVIEW\'s writes would '
    "refresh tv_profile, which needs the TRIGGER privilege"
)


class TestATviewReadingAnotherRolesTable:
    def test_names_the_grant_that_lets_it(self) -> None:
        refused = _failed_on(psycopg.errors.InsufficientPrivilege(_TRIGGER_REFUSED))
        with pytest.raises(FraisierMigrationError) as excinfo:
            _migrate_up(_failing(refused))

        assert 'GRANT TRIGGER ON "public"."tv_user" TO' in (excinfo.value.recovery_hint)

    def test_another_permission_refusal_keeps_the_general_advice(self) -> None:
        refused = _failed_on(
            psycopg.errors.InsufficientPrivilege("permission denied for table tb_x")
        )
        with pytest.raises(FraisierMigrationError) as excinfo:
            _migrate_up(_failing(refused))

        assert "GRANT TRIGGER" not in excinfo.value.recovery_hint

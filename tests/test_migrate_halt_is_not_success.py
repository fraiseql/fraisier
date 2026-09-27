"""A confiture run that halted is not a deploy that succeeded (#417).

On confiture up to 1.23.1, ``MigrateUpResult.has_errors`` is ``not success and
len(errors) > 0`` — it needs **both**.  Confiture halts the chain at a
``requires_superuser`` migration and returns ``success=False`` carrying *no*
errors, so ``has_errors`` is ``False`` and every predicate built on it reads
the halt as a clean run.

confiture 1.24.0 fixed its half: a halt now always carries an error, and
``has_errors`` is redefined as ``not success``.  The floor is ``>=1.0.0``, so
**both** shapes are reachable and ``has_errors`` means different things across
the supported range.  ``success`` does not move, which is why fraisier branches
on it — and why these tests pin that rather than a value that depends on which
confiture resolved.

These tests build **real** ``MigrateUpResult`` objects rather than mocks.  The
existing suite sets ``mock_result.has_errors = False`` on a ``MagicMock``, which
never executes the property, and that is exactly why this went unseen: a mock
cannot disagree with the assumption that produced it.
"""

from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from confiture.models.results import (
    MigrateUpResult,
    MigrationApplied,
    SkippedMigration,
)
from packaging.version import Version

if TYPE_CHECKING:
    from collections.abc import Callable

from fraisier.errors import MigrationError as FraisierMigrationError


def _halted_result() -> MigrateUpResult:
    """What confiture's apply loop returns on a superuser halt, up to 1.23.1.

    ``apply_loop.py`` appends to ``skipped_superuser``, fills
    ``pending_after_halt``, sets ``halted`` and breaks; the result it builds
    passes no ``errors`` argument at all.

    Still reachable: the floor is ``>=1.0.0``, so a project can resolve any
    confiture below 1.24.0 and get exactly this.  From 1.24.0 the migrator fills
    ``errors`` and ``migrate-up.schema.json`` requires one, so this shape is no
    longer *produced* there — only constructible, which is why the premise test
    below reads the installed version instead of assuming one.
    """
    return MigrateUpResult(
        success=False,
        migrations_applied=[],
        total_duration_ms=7,
        checksums_verified=True,
        dry_run=False,
        skipped_superuser=[
            SkippedMigration(
                version="20260101120000",
                name="grant_replication",
                reason=(
                    "requires_superuser=True; resolve with "
                    "`confiture migrate apply-as <role> 20260101120000`"
                ),
            )
        ],
        pending=["20260101130000", "20260101140000"],
    )


def _halted_result_1_24() -> MigrateUpResult:
    """The same halt as confiture >= 1.24.0 reports it.

    fraiseql/confiture#432, released in 1.24.0 on 2026-09-26, makes
    ``success=False`` always carry at least one error, and redefines
    ``has_errors`` as ``not success``.  The halt then arrives with its own
    message instead of an empty ``errors`` list.

    The wording is verbatim from ``core/_migrator/apply_loop.py``, pluralised as
    that source pluralises it, rather than paraphrased from the changelog.

    fraisier must fail on both shapes: the floor is ``>=1.0.0``, so a project
    can resolve either, and branching on ``success`` is the one reading that
    is correct for both.
    """
    return MigrateUpResult(
        success=False,
        migrations_applied=[],
        total_duration_ms=7,
        checksums_verified=True,
        dry_run=False,
        errors=[
            "Halted at 20260101120000_grant_replication: it declares "
            "requires_superuser=True. Apply it with `confiture migrate "
            "apply-as <role> 20260101120000`, then re-run `confiture migrate "
            "up`; 2 migrations left pending."
        ],
        pending=["20260101130000", "20260101140000"],
    )


def _clean_result() -> MigrateUpResult:
    """A run that really did finish."""
    return MigrateUpResult(
        success=True,
        migrations_applied=[
            MigrationApplied(
                version="20260101120000",
                name="create_widgets",
                duration_ms=3,
                rows_affected=0,
            )
        ],
        total_duration_ms=3,
        checksums_verified=True,
        dry_run=False,
    )


_HALT_CARRIES_ITS_ERROR = Version("1.24.0")
"""The confiture release that fills ``errors`` on a halt (confiture#432)."""


def _installed_confiture() -> Version:
    """The version of the confiture *library* imported into this process.

    Deliberately not ``doctor._confiture_cli_version``, which reads the
    ``confiture`` binary on PATH: that is the right source for the drift gate,
    which shells out, and the wrong one here.  These tests import
    ``MigrateUpResult`` directly, so the installed distribution is what decides
    which shape arrives.
    """
    return Version(importlib.metadata.version("fraiseql-confiture"))


def test_has_errors_is_not_a_stable_reading_of_a_halt() -> None:
    """The premise, pinned: ``has_errors`` changes meaning across the range.

    Without this the rest of the file proves nothing.  Asserting either value
    unconditionally would pin a premise that is false on half the confitures the
    floor admits — not hypothetically: this assertion read ``is False`` and
    failed the day 1.24.0 published.
    """
    result = _halted_result()
    assert result.success is False
    assert result.errors == []

    if _installed_confiture() >= _HALT_CARRIES_ITS_ERROR:
        assert result.has_errors is True, (
            "1.24.0 redefines has_errors as `not success`, so a halt is no "
            "longer invisible to it"
        )
    else:
        assert result.has_errors is False, (
            "the #417 premise: `not success and len(errors) > 0` needs both, so "
            "a halt carrying no errors reads as a clean run"
        )


@pytest.mark.parametrize(
    "shape",
    [_halted_result, _halted_result_1_24],
    ids=["pre_1_24", "1_24_and_later"],
)
def test_success_is_false_on_every_halt_shape(
    shape: Callable[[], MigrateUpResult],
) -> None:
    """The invariant fraisier reads, and the only one that holds on both sides.

    ``success`` is ``not applied.halted`` on every confiture in range, so it is
    ``False`` for either shape whichever version resolved.
    """
    assert shape().success is False


def _env() -> MagicMock:
    env = MagicMock()
    env.migration.view_helpers = "manual"
    env.migration.tracking_table = "tb_confiture"
    env.database_url = "postgresql:///testdb"
    return env


def _session(result: MigrateUpResult) -> MagicMock:
    session = MagicMock()
    session.up.return_value = result
    return session


def _wire(mock_migrator: MagicMock, result: MigrateUpResult) -> None:
    ctx = mock_migrator.from_config.return_value
    ctx.__enter__ = MagicMock(return_value=_session(result))
    ctx.__exit__ = MagicMock(return_value=False)


class TestMigrateUp:
    """The live path for every deploy strategy."""

    @patch("fraisier.dbops.confiture.Migrator")
    @patch("fraisier.dbops.confiture._load_env")
    def test_a_halt_does_not_report_success(self, load_env, migrator) -> None:
        from fraisier.dbops.confiture import migrate_up

        load_env.return_value = _env()
        _wire(migrator, _halted_result())

        with pytest.raises(FraisierMigrationError):
            migrate_up("confiture.yaml")

    @patch("fraisier.dbops.confiture.Migrator")
    @patch("fraisier.dbops.confiture._load_env")
    def test_the_failure_names_the_migration_that_stopped_the_chain(
        self, load_env, migrator
    ) -> None:
        from fraisier.dbops.confiture import migrate_up

        load_env.return_value = _env()
        _wire(migrator, _halted_result())

        with pytest.raises(FraisierMigrationError) as excinfo:
            migrate_up("confiture.yaml")

        message = str(excinfo.value)
        assert "20260101120000" in message, (
            "a halt that does not name the migration leaves the operator "
            f"nothing to act on: {message!r}"
        )

    @patch("fraisier.dbops.confiture.Migrator")
    @patch("fraisier.dbops.confiture._load_env")
    def test_the_failure_says_how_many_are_left_unapplied(
        self, load_env, migrator
    ) -> None:
        from fraisier.dbops.confiture import migrate_up

        load_env.return_value = _env()
        _wire(migrator, _halted_result())

        with pytest.raises(FraisierMigrationError) as excinfo:
            migrate_up("confiture.yaml")

        assert "2" in str(excinfo.value), "the two pending migrations go unreported"

    @patch("fraisier.dbops.confiture.Migrator")
    @patch("fraisier.dbops.confiture._load_env")
    def test_the_1_24_halt_shape_also_fails(self, load_env, migrator) -> None:
        """confiture#432, released in 1.24.0 — no longer forward compatibility.

        Once the halt carries its own message, ``error_summary`` is non-empty
        and fraisier reports confiture's wording rather than building its own.
        The verdict must not depend on which shape arrived.
        """
        from fraisier.dbops.confiture import migrate_up

        load_env.return_value = _env()
        _wire(migrator, _halted_result_1_24())

        with pytest.raises(FraisierMigrationError) as excinfo:
            migrate_up("confiture.yaml")

        assert "20260101120000" in str(excinfo.value)

    @patch("fraisier.dbops.confiture.Migrator")
    @patch("fraisier.dbops.confiture._load_env")
    def test_a_real_success_is_still_a_success(self, load_env, migrator) -> None:
        from fraisier.dbops.confiture import migrate_up

        load_env.return_value = _env()
        _wire(migrator, _clean_result())

        result = migrate_up("confiture.yaml")
        assert result.success is True
        assert result.steps_applied == 1


class TestRehearsal:
    """``pre_migrate_verify`` — the run that licenses the real one."""

    @patch("fraisier.dbops.confiture.Migrator")
    @patch("fraisier.dbops.confiture._load_env")
    def test_a_halted_rehearsal_does_not_pass(self, load_env, migrator) -> None:
        from fraisier.dbops.confiture import dry_run_execute

        load_env.return_value = _env()
        _wire(migrator, _halted_result())

        result = dry_run_execute("confiture.yaml")
        assert result.success is False, (
            "a rehearsal that halted licenses the real migrate; reporting it "
            "as passed means the verification verified less than it claims"
        )

    @patch("fraisier.dbops.confiture.Migrator")
    @patch("fraisier.dbops.confiture._load_env")
    def test_a_halted_rehearsal_says_why(self, load_env, migrator) -> None:
        from fraisier.dbops.confiture import dry_run_execute

        load_env.return_value = _env()
        _wire(migrator, _halted_result())

        result = dry_run_execute("confiture.yaml")
        assert any("20260101120000" in str(e) for e in result.errors), (
            f"the halted migration is not named: {result.errors!r}"
        )

    @patch("fraisier.dbops.confiture.Migrator")
    @patch("fraisier.dbops.confiture._load_env")
    def test_a_clean_rehearsal_still_passes(self, load_env, migrator) -> None:
        from fraisier.dbops.confiture import dry_run_execute

        load_env.return_value = _env()
        _wire(migrator, _clean_result())

        assert dry_run_execute("confiture.yaml").success is True

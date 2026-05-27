"""Recovery hints in partial-state errors (#221 item 7, bundle A phase 02).

Errors raised from code paths that may leave the system in a
partially-applied state carry a ``recovery_hint`` field. When set, it
renders in ``str(err)`` as a trailing ``Recover with:`` line so agents
and operators don't have to grep memory files to figure out the next
move.

The hints themselves come from ``fraisier.errors.RECOVERY_HINTS`` keyed
by *scenario tag* (not class name) so one error class can map to
multiple scenarios and the lookup is decoupled from the type hierarchy.
"""

from __future__ import annotations

import pytest

from fraisier.errors import (
    RECOVERY_HINTS,
    HealthCheckError,
    MigrationError,
    MigrationPreflightError,
    RollbackError,
)


class TestRecoveryHintRendering:
    def test_per_instance_hint_renders_in_str(self):
        hint = "rollback restore; fix migrations; or `confiture migrate baseline`"
        err = MigrationPreflightError("12 migrations failed", recovery_hint=hint)
        assert "Recover with: rollback restore" in str(err)

    def test_per_instance_hint_overrides_class_default(self):
        custom = "ad-hoc instructions for this site"
        err = HealthCheckError("503 from /health", recovery_hint=custom)
        # class default is the generic "service may still be starting" one
        assert "service may still be starting" not in str(err)
        assert f"Recover with: {custom}" in str(err)

    def test_passing_empty_string_suppresses_trailing_line(self):
        # Explicit suppression for sites that don't want the default
        # class hint to render either.
        err = HealthCheckError("bare error", recovery_hint="")
        assert "Recover with:" not in str(err)
        assert "bare error" in str(err)


class TestRecoveryHintsCatalog:
    """The canonical hints dict is keyed by scenario tag, not class name."""

    def test_migration_preflight_scenario_present(self):
        hint = RECOVERY_HINTS["migration_preflight"]
        assert "rollback" in hint.lower()
        assert "baseline" in hint.lower()

    def test_partial_migrate_scenario_present(self):
        hint = RECOVERY_HINTS["migrate_partial"]
        assert "rollback" in hint.lower()

    def test_health_check_unhealthy_scenario_present(self):
        hint = RECOVERY_HINTS["health_check_unhealthy"]
        assert "rollback" in hint.lower()

    def test_rollback_failed_scenario_present(self):
        hint = RECOVERY_HINTS["rollback_failed"]
        assert "manual" in hint.lower()

    def test_scenarios_are_keyed_by_scenario_not_class(self):
        # Lock the contract: keys are lowercase_snake scenario tags,
        # never `ErrorClassName` (which would break when classes are
        # renamed).
        for key in RECOVERY_HINTS:
            assert key.islower(), f"{key!r} should be lowercase snake_case"
            assert "Error" not in key, (
                f"{key!r} reads like a class name; use a scenario tag instead"
            )


class TestErrorClassesAcceptHintKwarg:
    """Each error class that may raise from a partial-state path accepts
    ``recovery_hint=`` in its ``__init__``."""

    @pytest.mark.parametrize(
        "cls",
        [MigrationPreflightError, MigrationError, HealthCheckError, RollbackError],
    )
    def test_accepts_kwarg(self, cls):
        err = cls("msg", recovery_hint="custom-hint")
        assert "Recover with: custom-hint" in str(err)


class TestCanonicalHintReachesRaiseSite:
    """Lock the wiring at the preflight raise site so the canonical hint
    survives all the way to ``str(err)`` — the contract #221 cites."""

    def test_preflight_raise_site_uses_canonical_hint(self, monkeypatch):
        # Mock run_migration_preflight to return a failed result and
        # confirm the strategy's _run_preflight wraps it in a
        # MigrationPreflightError whose str() carries the canonical hint.
        from typing import ClassVar

        from fraisier.strategies import _restore

        class _FakeMigration:
            version = "0001"
            name = "broken.sql"
            error = "syntax error near unexpected token"

        class _FakeResult:
            all_passed = False
            failure_count = 1
            failures: ClassVar[list[_FakeMigration]] = [_FakeMigration()]
            migrations: ClassVar[list[_FakeMigration]] = [_FakeMigration()]
            total_ms = 5

        def _fake_run_preflight(**_kwargs):
            return _FakeResult()

        monkeypatch.setattr(
            "fraisier.dbops.preflight.run_migration_preflight", _fake_run_preflight
        )

        class _PF:
            timeout_seconds = 1

        class _Config:
            preflight = _PF()

        strategy = _restore.RestoreMigrateStrategy.__new__(
            _restore.RestoreMigrateStrategy
        )
        strategy._config = _Config()
        strategy._admin_url = "postgresql://test"

        from pathlib import Path

        with pytest.raises(MigrationPreflightError) as exc_info:
            strategy._run_preflight(
                backup_path=Path("/tmp/x.dump"),
                confiture_config=Path("/tmp/c.yaml"),
                migrations_dir=Path("/tmp/m"),
            )

        text = str(exc_info.value)
        assert "Recover with:" in text
        assert "rollback" in text.lower()
        assert "baseline" in text.lower()

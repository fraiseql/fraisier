"""Unit tests for RestoreMigrateStrategy preflight integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.dbops.preflight import MigrationCheck, MigrationPreflightResult
from fraisier.errors import MigrationPreflightError
from fraisier.strategies._restore import RestoreConfig, RestoreMigrateStrategy

# Patch targets: functions are imported locally inside execute(), so patch at source.
_P_FIND_BACKUP = "fraisier.dbops.restore.find_latest_backup"
_P_VALIDATE_AGE = "fraisier.dbops.restore.validate_backup_age"
_P_RESTORE = "fraisier.dbops.restore.restore_backup"
_P_TERMINATE = "fraisier.dbops.operations.terminate_backends"
_P_DROP = "fraisier.dbops.operations.drop_db"
_P_CREATE = "fraisier.dbops.operations.create_db"
_P_MIGRATE_UP = "fraisier.strategies._restore.migrate_up"
_P_PREFLIGHT = "fraisier.dbops.preflight.run_migration_preflight"


def _make_strategy(preflight_enabled: bool = True) -> RestoreMigrateStrategy:
    from fraisier.config.schema import PreflightConfig

    cfg = RestoreConfig(
        db_name="mydb",
        backup_dir=Path("/backups"),
        preflight=PreflightConfig(enabled=preflight_enabled),
    )
    return RestoreMigrateStrategy(
        cfg,
        admin_url="postgresql://admin@localhost/postgres",
    )


def _passing_result() -> MigrationPreflightResult:
    return MigrationPreflightResult(
        migrations=[MigrationCheck(version="001", name="ok", passed=True, time_ms=50)],
        schema_extraction_ms=500,
        total_ms=600,
    )


def _failing_result() -> MigrationPreflightResult:
    return MigrationPreflightResult(
        migrations=[
            MigrationCheck(version="001", name="ok", passed=True, time_ms=50),
            MigrationCheck(
                version="002",
                name="bad",
                passed=False,
                error="column does not exist",
            ),
        ],
        schema_extraction_ms=500,
        total_ms=600,
    )


def _false_positive_result() -> MigrationPreflightResult:
    """Earlier non-transactional migration skipped; later dependent fails (#250)."""
    return MigrationPreflightResult(
        migrations=[
            MigrationCheck(
                version="20240101120000",
                name="add_idx_concurrently",
                passed=False,
                skipped=True,
                skipped_reason="non-transactional: cannot run inside SAVEPOINT",
            ),
            MigrationCheck(
                version="20240101130000",
                name="dependent_view",
                passed=False,
                error='relation "public.widgets" does not exist',
            ),
        ],
        schema_extraction_ms=500,
        total_ms=600,
    )


_BACKUP_FILE = Path("/backups/latest.dump")


def _base_patches(preflight_result: MigrationPreflightResult | None = None):
    """Return list of patches for a happy-path execute() run."""
    patches = [
        patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
        patch(_P_VALIDATE_AGE, return_value=True),
        patch(_P_TERMINATE),
        patch(_P_DROP, return_value=(0, "", "")),
        patch(_P_CREATE, return_value=(0, "", "")),
        patch(_P_RESTORE, return_value=MagicMock(success=True)),
        patch(_P_MIGRATE_UP, return_value=MagicMock(steps_applied=1)),
    ]
    if preflight_result is not None:
        patches.append(patch(_P_PREFLIGHT, return_value=preflight_result))
    return patches


# ---------------------------------------------------------------------------
# PreflightEnabled / Disabled
# ---------------------------------------------------------------------------


class TestPreflightEnabled:
    def test_enabled_by_default(self):
        strategy = _make_strategy(preflight_enabled=True)
        assert strategy._preflight_enabled() is True

    def test_disabled_when_config_false(self):
        strategy = _make_strategy(preflight_enabled=False)
        assert strategy._preflight_enabled() is False


# ---------------------------------------------------------------------------
# _run_preflight
# ---------------------------------------------------------------------------


class TestRunPreflight:
    def test_passes_silently_when_all_pass(self):
        strategy = _make_strategy()
        with patch(_P_PREFLIGHT, return_value=_passing_result()):
            strategy._run_preflight(
                backup_path=Path("/backup.dump"),
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

    def test_raises_migration_preflight_error_on_failure(self):
        strategy = _make_strategy()
        with (
            patch(_P_PREFLIGHT, return_value=_failing_result()),
            pytest.raises(MigrationPreflightError) as exc_info,
        ):
            strategy._run_preflight(
                backup_path=Path("/backup.dump"),
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        assert "column does not exist" in str(exc_info.value)
        assert exc_info.value.preflight_result is not None

    def test_error_contains_failure_count(self):
        strategy = _make_strategy()
        with (
            patch(_P_PREFLIGHT, return_value=_failing_result()),
            pytest.raises(MigrationPreflightError) as exc_info,
        ):
            strategy._run_preflight(
                backup_path=Path("/backup.dump"),
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        assert "1 of 2" in str(exc_info.value)

    def test_error_code_is_preflight_failed(self):
        strategy = _make_strategy()
        with (
            patch(_P_PREFLIGHT, return_value=_failing_result()),
            pytest.raises(MigrationPreflightError) as exc_info,
        ):
            strategy._run_preflight(
                backup_path=Path("/backup.dump"),
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        assert exc_info.value.code == "MIGRATION_PREFLIGHT_FAILED"
        assert exc_info.value.recoverable is True

    def test_genuine_failure_keeps_standard_hint(self):
        """A real failure (no skipped migration) does not advertise --skip-preflight."""
        strategy = _make_strategy()
        with (
            patch(_P_PREFLIGHT, return_value=_failing_result()),
            pytest.raises(MigrationPreflightError) as exc_info,
        ):
            strategy._run_preflight(
                backup_path=Path("/backup.dump"),
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        assert "false positive" not in str(exc_info.value).lower()
        assert "--skip-preflight" not in exc_info.value.recovery_hint

    def test_false_positive_appends_diagnostic_and_hint(self):
        """Skipped non-transactional + dependent failure → diagnostic + escape hatch."""
        strategy = _make_strategy()
        with (
            patch(_P_PREFLIGHT, return_value=_false_positive_result()),
            pytest.raises(MigrationPreflightError) as exc_info,
        ):
            strategy._run_preflight(
                backup_path=Path("/backup.dump"),
                confiture_config=Path("confiture.yaml"),
                migrations_dir=Path("db/migrations"),
            )

        err = exc_info.value
        assert "false positive" in str(err).lower()
        assert "non-transactional" in str(err)
        assert "--skip-preflight" in err.recovery_hint
        # The structured result is still attached for programmatic callers.
        assert err.preflight_result is not None


# ---------------------------------------------------------------------------
# execute() integration
# ---------------------------------------------------------------------------


class TestExecutePreflight:
    def test_restore_not_called_on_preflight_failure(self):
        strategy = _make_strategy()

        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=_failing_result()),
            patch(_P_TERMINATE),
            patch(_P_DROP, return_value=(0, "", "")),
            patch(_P_CREATE, return_value=(0, "", "")),
            patch(_P_RESTORE) as mock_restore,
            patch(_P_MIGRATE_UP, return_value=MagicMock(steps_applied=0)),
            pytest.raises(MigrationPreflightError),
        ):
            strategy.execute(Path("confiture.yaml"))

        mock_restore.assert_not_called()

    def test_execute_succeeds_when_preflight_passes(self):
        strategy = _make_strategy()
        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=_passing_result()),
            patch(_P_TERMINATE),
            patch(_P_DROP, return_value=(0, "", "")),
            patch(_P_CREATE, return_value=(0, "", "")),
            patch(_P_RESTORE, return_value=MagicMock(success=True)),
            patch(_P_MIGRATE_UP, return_value=MagicMock(steps_applied=1)),
        ):
            result = strategy.execute(Path("confiture.yaml"))

        assert result.success is True

    def test_interdependent_preflight_does_not_block_restore(self):
        """Issue #250: an inter-dependent pending pair that preflights green
        (V2's view over V1's table) must not raise — restore proceeds."""
        strategy = _make_strategy()
        interdependent = MigrationPreflightResult(
            migrations=[
                MigrationCheck(
                    version="20240101120000",
                    name="create_widgets",
                    passed=True,
                    time_ms=10,
                ),
                MigrationCheck(
                    version="20240101130000",
                    name="add_widgets_view",
                    passed=True,
                    time_ms=8,
                ),
            ],
            schema_extraction_ms=500,
            total_ms=600,
        )
        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, return_value=interdependent),
            patch(_P_TERMINATE),
            patch(_P_DROP, return_value=(0, "", "")),
            patch(_P_CREATE, return_value=(0, "", "")),
            patch(_P_RESTORE, return_value=MagicMock(success=True)) as mock_restore,
            patch(_P_MIGRATE_UP, return_value=MagicMock(steps_applied=2)),
        ):
            result = strategy.execute(Path("confiture.yaml"))

        assert result.success is True
        mock_restore.assert_called_once()

    def test_skip_preflight_bypasses_check(self):
        strategy = _make_strategy()
        preflight_called = False

        def spy_preflight(*args, **kwargs):
            nonlocal preflight_called
            preflight_called = True
            return _passing_result()

        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, side_effect=spy_preflight),
            patch(_P_TERMINATE),
            patch(_P_DROP, return_value=(0, "", "")),
            patch(_P_CREATE, return_value=(0, "", "")),
            patch(_P_RESTORE, return_value=MagicMock(success=True)),
            patch(_P_MIGRATE_UP, return_value=MagicMock(steps_applied=1)),
        ):
            result = strategy.execute(Path("confiture.yaml"), skip_preflight=True)

        assert not preflight_called
        assert result.success is True

    def test_preflight_skipped_when_disabled_in_config(self):
        strategy = _make_strategy(preflight_enabled=False)
        preflight_called = False

        def spy_preflight(*args, **kwargs):
            nonlocal preflight_called
            preflight_called = True
            return _passing_result()

        with (
            patch(_P_FIND_BACKUP, return_value=_BACKUP_FILE),
            patch(_P_VALIDATE_AGE, return_value=True),
            patch(_P_PREFLIGHT, side_effect=spy_preflight),
            patch(_P_TERMINATE),
            patch(_P_DROP, return_value=(0, "", "")),
            patch(_P_CREATE, return_value=(0, "", "")),
            patch(_P_RESTORE, return_value=MagicMock(success=True)),
            patch(_P_MIGRATE_UP, return_value=MagicMock(steps_applied=1)),
        ):
            result = strategy.execute(Path("confiture.yaml"))

        assert not preflight_called
        assert result.success is True


# ---------------------------------------------------------------------------
# MigrationPreflightError
# ---------------------------------------------------------------------------


class TestMigrationPreflightError:
    def test_error_has_structured_result(self):
        result = _failing_result()
        err = MigrationPreflightError("preflight failed", preflight_result=result)
        assert err.preflight_result is result
        assert err.recoverable is True

    def test_error_without_result(self):
        err = MigrationPreflightError("preflight failed")
        assert err.preflight_result is None

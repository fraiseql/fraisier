"""The archive is proven readable before the database is dropped (#343).

`RestoreMigrateStrategy.execute` stopped the service, terminated every
backend, and dropped and recreated the database — and only then handed the
file to `pg_restore`. A dump that `pg_restore --list` rejects in under a
second therefore cost the staging database it was supposed to replace, which
is the #339 incident: a truncated dump restored into staging, emptying it.

Steps 1 and 2 look like validation and are not. `find_latest_backup` sorts by
mtime and `validate_backup_age` compares mtime to a cutoff; neither opens the
file. Preflight is the only earlier read and does not close the hole — it is
skippable by flag, disableable by config, and extracts the **schema**, so a
dump truncated inside the data section passes it and fails at step 6.

These tests assert on the *call sequence*, not on the error message. A
message can be right while the database is already gone.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.config.schema import PreflightConfig
from fraisier.dbops.archive import ArchiveCheck, ArchiveVerdict
from fraisier.dbops.confiture import MigrationResult
from fraisier.dbops.restore import RestoreResult
from fraisier.errors import DatabaseError
from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy

_ADMIN_URL = "postgresql://postgres@localhost:5432/postgres"

_TRUNCATED = ArchiveCheck(
    ArchiveVerdict.INVALID,
    "pg_restore: error: could not read from input file: end of file",
)
_UNVERIFIABLE = ArchiveCheck(
    ArchiveVerdict.UNVERIFIABLE, "pg_restore not found on PATH"
)
_GOOD = ArchiveCheck(ArchiveVerdict.VALID, "")


def _strategy(*, preflight_enabled: bool = False, service=None, **overrides):
    config = RestoreConfig(
        db_name="staging_db",
        backup_dir=Path("/backup/production"),
        preflight=PreflightConfig(enabled=preflight_enabled),
        **overrides,
    )
    return RestoreMigrateStrategy(
        config,
        admin_url=_ADMIN_URL,
        service_manager=service,
        service_name="staging.service" if service else None,
    )


class _Destructive:
    """Every mock whose call would mean the database is already committed."""

    def __init__(self, stack):
        self.drop = stack.enter_context(patch("fraisier.dbops.operations.drop_db"))
        self.create = stack.enter_context(patch("fraisier.dbops.operations.create_db"))
        self.terminate = stack.enter_context(
            patch("fraisier.dbops.operations.terminate_backends")
        )
        self.restore = stack.enter_context(
            patch("fraisier.dbops.restore.restore_backup")
        )
        self.migrate = stack.enter_context(
            patch("fraisier.strategies._restore.migrate_up")
        )
        self.drop.return_value = (0, "", "")
        self.create.return_value = (0, "", "")
        self.restore.return_value = RestoreResult(success=True, duration_seconds=0.1)
        self.migrate.return_value = MigrationResult(success=True, steps_applied=0)

    def assert_untouched(self):
        assert not self.drop.called, "database was dropped"
        assert not self.create.called, "database was recreated"
        assert not self.terminate.called, "backends were terminated"
        assert not self.restore.called, "pg_restore ran"
        assert not self.migrate.called, "migrations ran"


class TestBadArchiveNeverReachesTheDrop:
    def test_invalid_archive_aborts_before_any_destructive_step(self):
        import contextlib

        service = MagicMock()
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch(
                    "fraisier.dbops.restore.find_latest_backup",
                    return_value=Path("/backup/production/truncated.dump"),
                )
            )
            stack.enter_context(
                patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
            )
            stack.enter_context(
                patch("fraisier.dbops.archive.verify_archive", return_value=_TRUNCATED)
            )
            destructive = _Destructive(stack)

            with pytest.raises(DatabaseError) as excinfo:
                _strategy(service=service).execute(Path("confiture.yaml"))

            destructive.assert_untouched()
            assert not service.stop.called, "service was stopped"

        assert "end of file" in str(excinfo.value)
        assert "truncated.dump" in str(excinfo.value)

    def test_unverifiable_archive_lets_the_restore_proceed(self, caplog):
        """A host without pg_restore does not lose the ability to restore."""
        import contextlib
        import logging

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch(
                    "fraisier.dbops.restore.find_latest_backup",
                    return_value=Path("/backup/production/db.dump"),
                )
            )
            stack.enter_context(
                patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
            )
            stack.enter_context(
                patch(
                    "fraisier.dbops.archive.verify_archive", return_value=_UNVERIFIABLE
                )
            )
            destructive = _Destructive(stack)
            with caplog.at_level(
                logging.WARNING, logger="fraisier.strategies._restore"
            ):
                result = _strategy().execute(Path("confiture.yaml"))

            assert result.success is True
            assert destructive.restore.called

        assert "pg_restore not found on PATH" in caplog.text

    def test_valid_archive_restores_exactly_as_before(self):
        import contextlib

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch(
                    "fraisier.dbops.restore.find_latest_backup",
                    return_value=Path("/backup/production/db.dump"),
                )
            )
            stack.enter_context(
                patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
            )
            stack.enter_context(
                patch("fraisier.dbops.archive.verify_archive", return_value=_GOOD)
            )
            destructive = _Destructive(stack)
            result = _strategy().execute(Path("confiture.yaml"))

            assert result.success is True
            assert destructive.drop.called
            assert destructive.restore.called


class TestNeitherEscapeHatchSkipsTheCheck:
    """`--skip-preflight` exists for emergency restores. This is not preflight.

    An emergency restore may skip migration validation; it may not skip "is
    this a file pg_restore can read", because that check is the only thing
    standing between a corrupt dump and the database it is about to drop.
    """

    def _run_with(self, *, skip_preflight: bool, preflight_enabled: bool):
        import contextlib

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch(
                    "fraisier.dbops.restore.find_latest_backup",
                    return_value=Path("/backup/production/truncated.dump"),
                )
            )
            stack.enter_context(
                patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
            )
            stack.enter_context(
                patch("fraisier.dbops.archive.verify_archive", return_value=_TRUNCATED)
            )
            stack.enter_context(
                patch(
                    "fraisier.dbops.preflight.run_migration_preflight",
                    side_effect=AssertionError("preflight should not be reached"),
                )
            )
            destructive = _Destructive(stack)
            with pytest.raises(DatabaseError):
                _strategy(preflight_enabled=preflight_enabled).execute(
                    Path("confiture.yaml"), skip_preflight=skip_preflight
                )
            destructive.assert_untouched()

    def test_skip_preflight_does_not_skip_the_archive_check(self):
        self._run_with(skip_preflight=True, preflight_enabled=True)

    def test_preflight_disabled_does_not_skip_the_archive_check(self):
        self._run_with(skip_preflight=False, preflight_enabled=False)


class TestExplicitPathIsCheckedToo:
    """`--from-backup` skips the *age* rule. That exemption is about age.

    A path an operator typed is not a path an operator verified, and the
    database is dropped either way.
    """

    def test_explicit_backup_path_is_verified(self):
        import contextlib

        explicit = Path("/tmp/handed-over.dump")
        with contextlib.ExitStack() as stack:
            find = stack.enter_context(
                patch("fraisier.dbops.restore.find_latest_backup")
            )
            stack.enter_context(
                patch("fraisier.dbops.archive.verify_archive", return_value=_TRUNCATED)
            )
            destructive = _Destructive(stack)

            with pytest.raises(DatabaseError):
                _strategy(backup_path=explicit).execute(Path("confiture.yaml"))

            assert not find.called, "explicit path should not be resolved by glob"
            destructive.assert_untouched()

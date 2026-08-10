"""The restore leaves a receipt proving *this* run rewrote the database (#358).

#358 asks for a check that the data arrived, and reaches for row counts. The
failure it was filed against cannot be seen by counting anything: a staging
database that was never rewritten holds yesterday's data, and yesterday's data
has entirely correct counts. What distinguishes it is not *what* is in the
database but *when* it got there, and who put it there.

So the pipeline mints a token per run and writes it into the database it just
restored. A run that never happened leaves the previous run's token behind — and
a token is the one thing presence alone cannot fake, which is why the check is
against a token rather than against the existence of a row.

The receipt is bookkeeping, and bookkeeping does not get to fail a restore that
passed every check it has. A write that fails is reported as *unverified*, never
as verified and never as a failure.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fraisier.config.schema import PreflightConfig
from fraisier.dbops.archive import ArchiveCheck, ArchiveVerdict
from fraisier.dbops.confiture import MigrationResult
from fraisier.dbops.receipt import ActuationCheck, ActuationVerdict, RestoreReceipt
from fraisier.dbops.restore import RestoreResult
from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy

_ADMIN_URL = "postgresql://postgres@localhost:5432/postgres"
_BACKUP = Path("/backup/production/db.dump")


def _actuated(run_id: str = "tok") -> ActuationCheck:
    return ActuationCheck(
        ActuationVerdict.ACTUATED,
        "ok",
        RestoreReceipt(run_id, str(_BACKUP), 4096, MagicMock(), 1.0),
    )


def _execute(
    *,
    write_result: str | None = None,
    verify_result: ActuationCheck | None = None,
    min_tables: int = 0,
    order: list[str] | None = None,
):
    """Drive the strategy to completion with the receipt seam mocked."""
    check = ArchiveCheck(ArchiveVerdict.VALID, "", {"public": 12})

    def _note(name, value):
        def _fn(*_args, **_kwargs):
            if order is not None:
                order.append(name)
            return value

        return _fn

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("fraisier.dbops.restore.find_latest_backup", return_value=_BACKUP)
        )
        stack.enter_context(
            patch("fraisier.dbops.restore.validate_backup_age", return_value=True)
        )
        stack.enter_context(
            patch("fraisier.dbops.archive.verify_archive", return_value=check)
        )
        stack.enter_context(
            patch.object(Path, "stat", return_value=MagicMock(st_size=4096))
        )
        for target, value in (
            ("fraisier.dbops.operations.drop_db", (0, "", "")),
            ("fraisier.dbops.operations.create_db", (0, "", "")),
            ("fraisier.dbops.operations.terminate_backends", None),
        ):
            stack.enter_context(patch(target, return_value=value))
        stack.enter_context(
            patch(
                "fraisier.dbops.restore.restore_backup",
                side_effect=_note("restore", RestoreResult(True, duration_seconds=0.1)),
            )
        )
        stack.enter_context(
            patch(
                "fraisier.strategies._restore.migrate_up",
                side_effect=_note("migrate", MigrationResult(True, steps_applied=3)),
            )
        )
        stack.enter_context(
            patch(
                "fraisier.dbops.restore.validate_table_count", return_value=(True, 99)
            )
        )
        writer = stack.enter_context(
            patch(
                "fraisier.dbops.receipt.write_receipt",
                side_effect=_note("receipt", write_result),
            )
        )
        verifier = stack.enter_context(
            patch(
                "fraisier.dbops.receipt.verify_actuation",
                return_value=verify_result or _actuated(),
            )
        )
        strategy = RestoreMigrateStrategy(
            RestoreConfig(
                db_name="staging_db",
                backup_dir=Path("/backup/production"),
                preflight=PreflightConfig(enabled=False),
                min_tables=min_tables,
            ),
            admin_url=_ADMIN_URL,
        )
        result = strategy.execute(Path("confiture.yaml"))
    return writer, verifier, result


class TestTheRunLeavesAReceipt:
    def test_the_receipt_is_written_once(self):
        writer, _, _ = _execute()

        assert writer.call_count == 1

    def test_the_receipt_is_written_after_the_migration(self):
        """A receipt written earlier would name a run that had not finished.

        The restore succeeding is not the claim; the *pipeline* succeeding is.
        A migration failure after an early write would leave a receipt asserting
        an outcome that never happened — the class of lie #356 was about.
        """
        order: list[str] = []
        _execute(order=order)

        assert order == ["restore", "migrate", "receipt"]

    def test_the_receipt_names_the_backup_that_was_restored(self):
        """Which archive, not merely that some archive was loaded."""
        writer, _, _ = _execute()

        receipt = writer.call_args.kwargs["receipt"]
        assert receipt.backup_path == str(_BACKUP)
        assert receipt.backup_bytes == 4096

    def test_the_receipt_goes_into_the_restored_database(self):
        writer, _, _ = _execute()

        assert writer.call_args.args[0] == "staging_db"
        assert writer.call_args.kwargs["connection_url"] == _ADMIN_URL

    def test_each_run_mints_its_own_token(self):
        """Presence is not proof: a no-op leaves the previous run's token."""
        first, _, _ = _execute()
        second, _, _ = _execute()

        assert (
            first.call_args.kwargs["receipt"].run_id
            != second.call_args.kwargs["receipt"].run_id
        )

    def test_the_token_is_read_back_out_of_the_database(self):
        """A round trip, not a variable the pipeline set and then trusted."""
        writer, verifier, _ = _execute()

        minted = writer.call_args.kwargs["receipt"].run_id
        assert verifier.call_args.kwargs["expected_run_id"] == minted

    def test_the_result_carries_the_verified_receipt(self):
        _, _, result = _execute(verify_result=_actuated("tok-9"))

        assert result.actuation is not None
        assert result.actuation.is_actuated is True
        assert result.actuation.receipt is not None
        assert result.actuation.receipt.run_id == "tok-9"


class TestBookkeepingDoesNotFailARestore:
    """The restore passed every check it has. A receipt is not one of them."""

    def test_a_write_failure_leaves_the_restore_successful(self):
        _, _, result = _execute(write_result="permission denied for schema fraisier")

        assert result.success is True

    def test_a_write_failure_is_reported_as_unverified(self):
        """Not checked, never checked-and-passed."""
        _, _, result = _execute(write_result="permission denied for schema fraisier")

        assert result.actuation is not None
        assert result.actuation.verdict is ActuationVerdict.UNVERIFIABLE
        assert result.actuation.is_actuated is False
        assert result.actuation.is_bad is False
        assert "permission denied" in result.actuation.detail

    def test_a_failed_write_is_not_then_read_back(self):
        """Reading back a receipt known not to have been written proves nothing."""
        _, verifier, _ = _execute(write_result="disk full")

        assert verifier.call_count == 0

    def test_a_read_back_mismatch_does_not_fail_the_restore_either(self):
        stale = ActuationCheck(ActuationVerdict.STALE, "someone else's receipt")
        _, _, result = _execute(verify_result=stale)

        assert result.success is True
        assert result.actuation is not None
        assert result.actuation.is_bad is True


class TestTheConsoleSaysWhetherItWasProven:
    """An operator reading "Restore complete" should learn what proved it."""

    @staticmethod
    def _config():
        config = MagicMock()
        config.get_fraise.return_value = {"type": "api"}
        config.get_fraise_environment.return_value = {
            "type": "api",
            "app_path": "/var/www/api",
            "systemd_service": "api.staging.service",
            "database": {
                "name": "mydb_staging",
                "strategy": "restore_migrate",
                "admin_url": _ADMIN_URL,
                "confiture_config": "confiture.yaml",
                "restore": {
                    "backup_dir": "/backup/production",
                    "backup_pattern": "*.dump",
                    "max_age_hours": 48.0,
                },
            },
        }
        config._config = {"backup": {}}
        config.deployment = MagicMock()
        config.deployment.get_strategy.return_value = "restore_migrate"
        config.list_fraises_detailed.return_value = []
        return config

    def _invoke(self, actuation):
        from click.testing import CliRunner

        from fraisier.cli.main import main

        result_mock = MagicMock(
            success=True,
            migrations_applied=0,
            total_duration_seconds=0.0,
            restore_duration_seconds=0.0,
            migration_duration_seconds=0.0,
            schema_floor=("public", 12),
            unchecked_schemas=(),
            actuation=actuation,
        )
        with (
            patch("fraisier.cli.main.get_config", return_value=self._config()),
            patch("fraisier.locking.deployment_lock", MagicMock()),
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch(
                "fraisier.strategies.RestoreMigrateStrategy.execute",
                return_value=result_mock,
            ),
            patch("fraisier.systemd.SystemdServiceManager"),
            patch(
                "fraisier.dbops.restore.find_latest_backup",
                return_value="/backup/production/x.dump",
            ),
            patch("fraisier.dbops.restore.validate_backup_age"),
            patch(
                "fraisier.post_migrate.run_configured_post_migrate", return_value=None
            ),
        ):
            return CliRunner().invoke(main, ["db", "restore", "api", "staging"])

    def test_a_verified_receipt_names_the_run(self):
        res = self._invoke(_actuated("abc123def"))

        assert res.exit_code == 0, res.output
        assert "abc123def" in res.output

    def test_an_unverified_receipt_says_so_without_claiming_a_failure(self):
        res = self._invoke(
            ActuationCheck(ActuationVerdict.UNVERIFIABLE, "psql not found on PATH")
        )

        assert res.exit_code == 0, res.output
        assert "not verified" in res.output.lower()
        assert "psql not found" in res.output

    def test_a_stale_receipt_is_surfaced_as_a_warning(self):
        res = self._invoke(
            ActuationCheck(ActuationVerdict.STALE, "staging still holds run yesterday")
        )

        assert "yesterday" in res.output

    def test_the_line_is_absent_when_the_result_predates_the_feature(self):
        """An older StrategyResult has no `actuation`; the CLI must not crash."""
        res = self._invoke(None)

        assert res.exit_code == 0, res.output


@pytest.mark.parametrize(
    "verdict",
    [ActuationVerdict.MISSING, ActuationVerdict.UNVERIFIABLE],
)
def test_silence_is_never_proof(verdict):
    """The two verdicts that say nothing must not read as the one that says yes."""
    check = ActuationCheck(verdict, "d")

    assert check.is_actuated is False
    assert check.is_bad is False

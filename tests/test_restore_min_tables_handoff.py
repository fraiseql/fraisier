"""The table-count floor is enforced where it is claimed to be (#343).

`restore_backup` passed `min_tables=0` into confiture's `RestoreOptions` under
a comment saying the strategy validates the count itself "after `migrate up`
(step 10), so confiture skips its own min-tables check". Step 10 is
`if cfg.min_tables > 0`, and `cfg.min_tables` is `restore_cfg.get("min_tables",
0)`. In the default configuration **neither** side validated, and a comment
said one did.

That is #341's lesson in a second costume. There, a green test listed
`ProtectHome=true` as *required* for a unit that could not exec because of it,
so the assertion read as evidence the unit was correct. Here a comment asserts
a guarantee by naming a check whose own condition disables it. Hand-off
comments are claims, not evidence.

The fix is deliberately *not* a non-zero default — that would fail restores
which legitimately produce few tables, on every host, on upgrade. A configured
floor is honoured, and an absent one is **stated** rather than silently claimed
away. And the honest limit, recorded because it is the reason the floor is
bookkeeping rather than the guard: a table-count floor cannot detect the #339
truncation. The schema restores, the tables exist, and they are empty. The
archive check in `test_restore_verifies_before_drop.py` is the guard.
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from fraisier.dbops.restore import restore_backup

_ADMIN_URL = "postgresql://postgres@localhost:5432/postgres"


def _restorer_returning_success():
    """Patch confiture's DatabaseRestorer and hand back the captured options."""
    restorer = MagicMock()
    restorer.return_value.restore.return_value = MagicMock(
        success=True,
        errors=[],
        matviews_deferred=None,
        matviews_refreshed=0,
        analyze_ran=False,
    )
    return restorer


class TestMinTablesReachesConfiture:
    def test_configured_floor_is_forwarded(self):
        restorer = _restorer_returning_success()
        with patch("fraisier.dbops.restore.DatabaseRestorer", restorer):
            restore_backup(
                backup_path="/backup/production/db.dump",
                db_name="staging_db",
                connection_url=_ADMIN_URL,
                min_tables=50,
            )
        options = restorer.return_value.restore.call_args[0][0]
        assert options.min_tables == 50

    def test_absent_floor_forwards_zero(self):
        """Zero is confiture's own default; forwarding it changes nothing there.

        What matters is that the value is no longer *hardcoded* to zero while a
        comment claims the check moved elsewhere.
        """
        restorer = _restorer_returning_success()
        with patch("fraisier.dbops.restore.DatabaseRestorer", restorer):
            restore_backup(
                backup_path="/backup/production/db.dump",
                db_name="staging_db",
                connection_url=_ADMIN_URL,
            )
        options = restorer.return_value.restore.call_args[0][0]
        assert options.min_tables == 0

    def test_strategy_passes_its_configured_floor_through(self):
        """The strategy's `min_tables` reaches `restore_backup`, not just step 10."""
        import contextlib

        from fraisier.config.schema import PreflightConfig
        from fraisier.dbops.archive import ArchiveCheck, ArchiveVerdict
        from fraisier.dbops.confiture import MigrationResult
        from fraisier.dbops.restore import RestoreResult
        from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy

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
                    "fraisier.dbops.archive.verify_archive",
                    return_value=ArchiveCheck(ArchiveVerdict.VALID, ""),
                )
            )
            for target, value in (
                ("fraisier.dbops.operations.drop_db", (0, "", "")),
                ("fraisier.dbops.operations.create_db", (0, "", "")),
                ("fraisier.dbops.operations.terminate_backends", None),
            ):
                stack.enter_context(patch(target, return_value=value))
            restore = stack.enter_context(
                patch(
                    "fraisier.dbops.restore.restore_backup",
                    return_value=RestoreResult(success=True, duration_seconds=0.1),
                )
            )
            stack.enter_context(
                patch(
                    "fraisier.strategies._restore.migrate_up",
                    return_value=MigrationResult(success=True, steps_applied=0),
                )
            )
            stack.enter_context(
                patch(
                    "fraisier.dbops.restore.validate_table_count",
                    return_value=(True, 120),
                )
            )
            strategy = RestoreMigrateStrategy(
                RestoreConfig(
                    db_name="staging_db",
                    backup_dir=Path("/backup/production"),
                    preflight=PreflightConfig(enabled=False),
                    min_tables=50,
                ),
                admin_url=_ADMIN_URL,
            )
            strategy.execute(Path("confiture.yaml"))

        assert restore.call_args.kwargs["min_tables"] == 50


class TestAbsentFloorIsStated:
    """An unconfigured floor is a declared state, not a silent one."""

    def _execute_with(self, min_tables: int, caplog):
        import contextlib

        from fraisier.config.schema import PreflightConfig
        from fraisier.dbops.archive import ArchiveCheck, ArchiveVerdict
        from fraisier.dbops.confiture import MigrationResult
        from fraisier.dbops.restore import RestoreResult
        from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy

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
                    "fraisier.dbops.archive.verify_archive",
                    return_value=ArchiveCheck(ArchiveVerdict.VALID, ""),
                )
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
                    return_value=RestoreResult(success=True, duration_seconds=0.1),
                )
            )
            stack.enter_context(
                patch(
                    "fraisier.strategies._restore.migrate_up",
                    return_value=MigrationResult(success=True, steps_applied=0),
                )
            )
            stack.enter_context(
                patch(
                    "fraisier.dbops.restore.validate_table_count",
                    return_value=(True, 120),
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
            with caplog.at_level(logging.INFO, logger="fraisier.strategies._restore"):
                strategy.execute(Path("confiture.yaml"))

    def test_no_floor_says_so(self, caplog):
        self._execute_with(0, caplog)
        assert "no table-count floor" in caplog.text.lower()

    def test_configured_floor_reports_the_count_it_passed(self, caplog):
        self._execute_with(50, caplog)
        assert "120 >= 50" in caplog.text
        assert "no table-count floor" not in caplog.text.lower()


class TestNoCommentClaimsADisabledCheck:
    """The false claim, removed and kept removed."""

    def test_restore_module_does_not_claim_step_10_covers_it(self):
        import fraisier.dbops.restore as module

        source = Path(module.__file__).read_text()
        assert "so confiture skips its own min-tables check" not in source


class TestTheCliSaysIt:
    """`log.info` reaches nobody here, so the declaration goes to the console.

    `fraisier` only calls `logging.basicConfig` under `-v`, and the root logger
    otherwise has no handler — so an INFO line is invisible interactively *and*
    absent from the journal, where `logging.lastResort` only passes WARNING and
    above. The generated timer unit runs `db restore` without `-v`. A statement
    an operator cannot read is not a declaration, so it goes through the
    console, whose stdout systemd captures.
    """

    @staticmethod
    def _config(min_tables: int | None):
        restore: dict = {
            "backup_dir": "/backup/production",
            "backup_pattern": "*.dump",
            "max_age_hours": 48.0,
        }
        if min_tables is not None:
            restore["min_tables"] = min_tables
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
                "restore": restore,
            },
        }
        config._config = {"backup": {}}
        config.deployment = MagicMock()
        config.deployment.get_strategy.return_value = "restore_migrate"
        config.list_fraises_detailed.return_value = []
        return config

    def _invoke(self, min_tables: int | None):
        from click.testing import CliRunner

        from fraisier.cli.main import main

        result_mock = MagicMock(
            success=True,
            migrations_applied=0,
            total_duration_seconds=0.0,
            restore_duration_seconds=0.0,
            migration_duration_seconds=0.0,
        )
        with (
            patch(
                "fraisier.cli.main.get_config",
                return_value=self._config(min_tables),
            ),
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
                "fraisier.post_migrate.run_configured_post_migrate",
                return_value=None,
            ),
        ):
            return CliRunner().invoke(main, ["db", "restore", "api", "staging"])

    def test_absent_floor_is_reported_on_the_console(self):
        res = self._invoke(None)
        assert res.exit_code == 0, res.output
        assert "no table-count floor" in res.output.lower()

    def test_configured_floor_is_not_reported_as_absent(self):
        res = self._invoke(100)
        assert res.exit_code == 0, res.output
        assert "no table-count floor" not in res.output.lower()

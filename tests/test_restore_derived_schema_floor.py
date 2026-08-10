"""The archive's own table of contents states the floor the restore must meet.

`min_tables` defaults to `0`, and `restore.py`'s floor is `if cfg.min_tables >
0`, so on a default configuration nothing counted anything: a restore that
produced almost nothing exited 0 with a yellow note. v0.61.0 made that state
*said* rather than enforced; this makes it enforced without asking an operator
to invent a number.

Two things this deliberately does not do:

- It does not sum schemas. TOC entries span every schema in the archive while
  the counter that verifies a restore takes one, so a whole-archive floor
  compared against a single schema's count is a guaranteed false failure —
  #356's own host keeps its heaps in `tenant`.
- It does not prove the data arrived. A dump truncated inside its data section
  restores a complete, empty schema and passes any count; `dbops/restore.py`
  says so in its own docstring. That hole stays open.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from fraisier.config.schema import PreflightConfig
from fraisier.dbops.archive import ArchiveCheck, ArchiveVerdict
from fraisier.dbops.confiture import MigrationResult
from fraisier.dbops.restore import RestoreResult, restore_backup
from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy

_ADMIN_URL = "postgresql://postgres@localhost:5432/postgres"


def _restorer_returning_success():
    restorer = MagicMock()
    restorer.return_value.restore.return_value = MagicMock(
        success=True,
        errors=[],
        matviews_deferred=None,
        matviews_refreshed=0,
        analyze_ran=False,
    )
    return restorer


def _execute(check: ArchiveCheck, *, min_tables: int = 0):
    """Drive the strategy to completion and hand back the restore_backup mock."""
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
            patch("fraisier.dbops.archive.verify_archive", return_value=check)
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
        counted = stack.enter_context(
            patch(
                "fraisier.dbops.restore.validate_table_count", return_value=(True, 120)
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
    return restore, result, counted


class TestTheArchivePicksItsPrincipalSchema:
    def test_the_schema_carrying_the_most_table_data_is_the_one_enforced(self):
        check = ArchiveCheck(ArchiveVerdict.VALID, "", {"public": 1, "tenant": 240})

        assert check.schema_floor == ("tenant", 240)

    def test_the_other_schemas_are_named_unchecked_not_summed(self):
        check = ArchiveCheck(
            ArchiveVerdict.VALID, "", {"public": 1, "tenant": 240, "audit": 3}
        )

        assert check.schema_floor == ("tenant", 240)
        assert check.unchecked_schemas == ("audit", "public")

    def test_a_tie_is_broken_deterministically(self):
        check = ArchiveCheck(ArchiveVerdict.VALID, "", {"beta": 7, "alpha": 7})

        assert check.schema_floor == ("alpha", 7)

    def test_a_schema_only_archive_states_no_floor(self):
        check = ArchiveCheck(ArchiveVerdict.VALID, "", {})

        assert check.schema_floor is None

    def test_an_unverifiable_archive_states_no_floor(self):
        check = ArchiveCheck(ArchiveVerdict.UNVERIFIABLE, "no pg_restore")

        assert check.schema_floor is None


class TestTheFloorReachesConfituresCounter:
    """confiture counts `pg_class WHERE relkind='r'` in a parameterised schema.

    That is apples-to-apples with `TABLE DATA`. fraisier's own counter
    (`information_schema.tables`, schema hardcoded to `public`) counts views and
    misses matviews, so it is wrong in both directions and is not used here.
    """

    def test_restore_backup_forwards_the_schema(self):
        restorer = _restorer_returning_success()
        with patch("fraisier.dbops.restore.DatabaseRestorer", restorer):
            restore_backup(
                backup_path="/backup/production/db.dump",
                db_name="staging_db",
                connection_url=_ADMIN_URL,
                min_tables=240,
                min_tables_schema="tenant",
            )
        options = restorer.return_value.restore.call_args[0][0]
        assert options.min_tables == 240
        assert options.min_tables_schema == "tenant"

    def test_restore_backup_defaults_to_public(self):
        restorer = _restorer_returning_success()
        with patch("fraisier.dbops.restore.DatabaseRestorer", restorer):
            restore_backup(
                backup_path="/backup/production/db.dump",
                db_name="staging_db",
                connection_url=_ADMIN_URL,
            )
        options = restorer.return_value.restore.call_args[0][0]
        assert options.min_tables_schema == "public"


class TestTheStrategyDerivesAndForwards:
    def test_the_derived_floor_and_schema_are_passed_through(self):
        restore, _, _counted = _execute(
            ArchiveCheck(ArchiveVerdict.VALID, "", {"public": 1, "tenant": 240})
        )

        assert restore.call_args.kwargs["min_tables"] == 240
        assert restore.call_args.kwargs["min_tables_schema"] == "tenant"

    def test_the_floor_is_never_the_sum_of_the_schemas(self):
        """241 against a `tenant`-only count is the false failure to avoid."""
        restore, _, _counted = _execute(
            ArchiveCheck(ArchiveVerdict.VALID, "", {"public": 1, "tenant": 240})
        )

        assert restore.call_args.kwargs["min_tables"] != 241

    def test_a_schema_only_archive_falls_back_to_the_configured_floor(self):
        restore, _, _counted = _execute(
            ArchiveCheck(ArchiveVerdict.VALID, "", {}), min_tables=50
        )

        assert restore.call_args.kwargs["min_tables"] == 50
        assert restore.call_args.kwargs["min_tables_schema"] == "public"

    def test_an_unverifiable_archive_falls_back_to_the_configured_floor(self):
        restore, _, _counted = _execute(
            ArchiveCheck(ArchiveVerdict.UNVERIFIABLE, "no pg_restore"), min_tables=50
        )

        assert restore.call_args.kwargs["min_tables"] == 50

    def test_an_unverifiable_archive_does_not_become_a_floor_of_zero_silently(self):
        _, result, _counted = _execute(
            ArchiveCheck(ArchiveVerdict.UNVERIFIABLE, "no pg_restore")
        )

        assert result.schema_floor is None

    def test_the_result_carries_what_was_checked(self):
        _, result, _counted = _execute(
            ArchiveCheck(ArchiveVerdict.VALID, "", {"public": 1, "tenant": 240})
        )

        assert result.schema_floor == ("tenant", 240)
        assert result.unchecked_schemas == ("public",)


class TestTheOperatorFloorKeepsItsOwnCheckpoint:
    """`min_tables` is post-migration; the derived floor is pre-migration.

    The two are not interchangeable: applied after migrations, an
    archive-derived floor false-fails on any migration that drops or renames a
    table. Applied before, an operator's floor cannot account for tables the
    migrations create.
    """

    def test_a_configured_floor_still_runs_after_migrate_up(self):
        _, _result, counted = _execute(
            ArchiveCheck(ArchiveVerdict.VALID, "", {"tenant": 240}), min_tables=50
        )

        assert counted.call_args.kwargs["min_threshold"] == 50

    def test_the_derived_floor_does_not_become_the_post_migration_floor(self):
        """240 tables in `tenant` must not be asserted against `public` later."""
        _, _result, counted = _execute(
            ArchiveCheck(ArchiveVerdict.VALID, "", {"tenant": 240})
        )

        assert counted.call_count == 0  # no operator floor configured

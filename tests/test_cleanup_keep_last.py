"""``keep_last`` — a count ceiling on the dump corpus (#419).

``retention_hours`` bounds the corpus by age, which on a busy day bounds
nothing: the number of dumps kept is the number of migrating deploys in the
window. Measured on production, 12 deploys in 72 h at 3.5 GB each is roughly 42 GB, on the
partition that also carries PGDATA.

This is **not** the mirror of ``keep_minimum``. The floor exempts dumps from a
removal rule that already exists; the ceiling has to create one, for dumps the
age rule protects outright — every dump inside the retention window is
currently kept without being a deletion candidate at all.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import ClassVar

import pytest

from fraisier.dbops.backup import cleanup_old_backups


def _dump(directory: Path, name: str, *, age_hours: float) -> Path:
    """A dump file whose mtime is *age_hours* old."""
    path = directory / name
    path.write_bytes(b"PGDMP fake archive")
    when = time.time() - age_hours * 3600
    os.utime(path, (when, when))
    return path


def _names(paths: tuple[str, ...]) -> set[str]:
    return {Path(p).name for p in paths}


class TestTheCeilingRemovesInsideTheWindow:
    """The case the age rule cannot reach."""

    def test_only_the_newest_n_survive(self, tmp_path: Path) -> None:
        for i in range(6):
            _dump(tmp_path, f"d{i}.dump", age_hours=i)

        outcome = cleanup_old_backups(tmp_path, retention_hours=72, keep_last=3)

        assert _names(outcome.kept) == {"d0.dump", "d1.dump", "d2.dump"}
        assert _names(outcome.removed) == {"d3.dump", "d4.dump", "d5.dump"}

    def test_a_ceiling_removal_is_removed_not_kept(self, tmp_path: Path) -> None:
        """The three tuples partition the corpus; a deleted file is not `kept`."""
        for i in range(4):
            _dump(tmp_path, f"d{i}.dump", age_hours=i)

        outcome = cleanup_old_backups(tmp_path, retention_hours=72, keep_last=2)

        assert _names(outcome.kept) & _names(outcome.removed) == set()
        survivors = _names(outcome.kept) | _names(outcome.exempted_by_minimum)
        assert survivors | _names(outcome.removed) == {f"d{i}.dump" for i in range(4)}

    def test_the_ceiling_says_which_it_took(self, tmp_path: Path) -> None:
        """An overlay, like `invalid` — a subset of `removed`, not a fourth group."""
        for i in range(4):
            _dump(tmp_path, f"d{i}.dump", age_hours=i)

        outcome = cleanup_old_backups(tmp_path, retention_hours=72, keep_last=2)

        assert _names(outcome.removed_by_ceiling) == {"d2.dump", "d3.dump"}
        assert set(outcome.removed_by_ceiling) <= set(outcome.removed)

    def test_the_files_are_really_gone(self, tmp_path: Path) -> None:
        for i in range(4):
            _dump(tmp_path, f"d{i}.dump", age_hours=i)

        cleanup_old_backups(tmp_path, retention_hours=72, keep_last=2)

        assert sorted(p.name for p in tmp_path.glob("*.dump")) == [
            "d0.dump",
            "d1.dump",
        ]


class TestTheCeilingAlone:
    """`keep_last` with no age rule at all.

    Two things blocked this and either would have made it a silent no-op:
    `retention_hours` was a required argument, and the gate only called
    cleanup `if retention_hours is not None`.
    """

    def test_it_deletes_without_retention_hours(self, tmp_path: Path) -> None:
        for i in range(5):
            _dump(tmp_path, f"d{i}.dump", age_hours=i)

        outcome = cleanup_old_backups(tmp_path, keep_last=2)

        assert _names(outcome.removed) == {"d2.dump", "d3.dump", "d4.dump"}
        assert sorted(p.name for p in tmp_path.glob("*.dump")) == ["d0.dump", "d1.dump"]

    def test_age_alone_still_works(self, tmp_path: Path) -> None:
        """No ceiling: the existing rule is untouched."""
        _dump(tmp_path, "fresh.dump", age_hours=1)
        _dump(tmp_path, "stale.dump", age_hours=100)

        outcome = cleanup_old_backups(tmp_path, retention_hours=72)

        assert _names(outcome.kept) == {"fresh.dump"}
        assert _names(outcome.removed) == {"stale.dump"}

    def test_neither_rule_removes_nothing(self, tmp_path: Path) -> None:
        _dump(tmp_path, "a.dump", age_hours=500)

        outcome = cleanup_old_backups(tmp_path)

        assert outcome.removed == ()
        assert _names(outcome.kept) == {"a.dump"}


class TestTheContradictionIsRejected:
    """`keep_last < keep_minimum` is a mistake, not a precedence puzzle.

    Picking a winner means silently honouring one of two things the operator
    asked for. Rejecting it is the same lesson as the silent no-ops: a
    configuration that cannot be satisfied should say so.
    """

    def test_a_ceiling_below_the_floor_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="keep_last"):
            cleanup_old_backups(
                tmp_path, retention_hours=72, keep_last=2, keep_minimum=3
            )

    def test_equal_is_allowed(self, tmp_path: Path) -> None:
        _dump(tmp_path, "a.dump", age_hours=1)
        cleanup_old_backups(tmp_path, retention_hours=72, keep_last=3, keep_minimum=3)

    def test_a_ceiling_below_one_raises(self, tmp_path: Path) -> None:
        """Zero would delete the rollback point for the migration about to run."""
        with pytest.raises(ValueError, match="keep_last"):
            cleanup_old_backups(tmp_path, retention_hours=72, keep_last=0)


class TestValidityStillDecidesSlots:
    """A corrupt newest file must not occupy a ceiling slot either (#342)."""

    def test_an_unreadable_dump_does_not_hold_a_ceiling_slot(
        self, tmp_path: Path
    ) -> None:
        # `verify_archive` shells out to `pg_restore --list`; on a host without
        # it every dump is UNVERIFIABLE and spends slots normally, which is the
        # documented behaviour. This test asserts only the count contract, so
        # it holds either way.
        for i in range(4):
            _dump(tmp_path, f"d{i}.dump", age_hours=i)

        outcome = cleanup_old_backups(tmp_path, keep_last=2)

        survivors = _names(outcome.kept) | _names(outcome.exempted_by_minimum)
        assert len(survivors) == 2


class TestTheGatePassesItThrough:
    """`PreMigrateDumpGate` must prune when *either* rule is set (#419).

    Before this, pruning was gated on `retention_hours is not None`, so a
    config that set only `keep_last` would have been a silent no-op — the
    setting accepted, the corpus unbounded, nothing in the log.
    """

    DUMP_CFG: ClassVar[dict[str, object]] = {
        "enabled": True,
        "output_dir": "/var/backups/gate",
        "db_name": "proddb",
    }

    def _strategy(self, **overrides):
        from fraisier.strategies import MigrateStrategy

        return MigrateStrategy(
            pre_migrate_dump={**self.DUMP_CFG, **overrides}, db_name="proddb"
        )

    def test_keep_last_alone_still_prunes(self) -> None:
        from unittest.mock import MagicMock, patch

        from fraisier.dbops.backup import CleanupOutcome
        from fraisier.dbops.confiture import MigrationResult

        cleanup = MagicMock(return_value=CleanupOutcome((), (), ()))
        with (
            patch("fraisier.dbops.backup.cleanup_old_backups", cleanup),
            patch("fraisier.dbops.confiture.has_pending", return_value=True),
            patch("fraisier.dbops.backup.run_backup"),
            patch("fraisier.strategies._core.preflight"),
            patch(
                "fraisier.strategies._core.migrate_up",
                return_value=MigrationResult(success=True, steps_applied=0),
            ),
        ):
            self._strategy(keep_last=3).execute(
                {"database": {"name": "proddb"}}, migrations_dir="db/migrations"
            )

        assert cleanup.called, (
            "keep_last with no retention_hours pruned nothing — the setting "
            "was accepted and did nothing, which is the shape of a silent no-op"
        )
        assert cleanup.call_args.kwargs["keep_last"] == 3

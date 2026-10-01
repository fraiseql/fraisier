"""One prune call for the dump gate, whoever asks for it (#420).

``pre_migrate_dump`` pruned only inside a deploy, so a quiet week left the whole
corpus on disk.  The fix is a prune that runs without one — and the issue is
specific that it is *the same call*: the gate's configured ``retention_hours`` and
``keep_last`` with ``keep_minimum=1``.  Two callers each spelling out that call
is how they drift apart, so there is one helper and these tests pin both callers
to it.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from fraisier.dbops.archive import ArchiveCheck, ArchiveVerdict
from fraisier.dbops.backup import BackupResult, prune_pre_migrate_corpus
from fraisier.strategies import MigrateStrategy

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def dumps_are_valid():
    """The fixtures write ``x``, not a pg_dump archive; say what they are."""
    with patch(
        "fraisier.dbops.backup.verify_archive",
        side_effect=lambda _p: ArchiveCheck(ArchiveVerdict.VALID, ""),
    ):
        yield


def corpus(directory: Path, **ages: float) -> Path:
    for name, hours in ages.items():
        path = directory / f"{name}.dump"
        path.write_text("x")
        when = time.time() - hours * 3600
        os.utime(path, (when, when))
    return directory


def names(paths: tuple[str, ...]) -> set[str]:
    return {p.rsplit("/", 1)[-1] for p in paths}


class TestTheHelper:
    def test_no_rule_is_none_not_an_empty_outcome(self, tmp_path: Path) -> None:
        """ "Nothing to do" and "did it and removed nothing" are different answers."""
        assert prune_pre_migrate_corpus({"output_dir": str(tmp_path)}) is None

    def test_retention_hours_expires_old_dumps(self, tmp_path: Path) -> None:
        corpus(tmp_path, fresh=1, old=100, older=200)
        config = {"output_dir": str(tmp_path), "retention_hours": 72}

        outcome = prune_pre_migrate_corpus(config)

        assert outcome is not None
        assert names(outcome.removed) == {"old.dump", "older.dump"}
        assert names(outcome.kept) == {"fresh.dump"}

    def test_keep_last_alone_is_a_complete_policy(self, tmp_path: Path) -> None:
        corpus(tmp_path, a=1, b=2, c=3)

        outcome = prune_pre_migrate_corpus(
            {"output_dir": str(tmp_path), "keep_last": 1}
        )

        assert outcome is not None
        assert names(outcome.removed) == {"b.dump", "c.dump"}

    def test_the_newest_dump_survives_an_all_expired_corpus(
        self, tmp_path: Path
    ) -> None:
        """``keep_minimum=1``: the next deploy's rollback point is never pruned.

        Mutation: with ``keep_minimum=0`` this corpus empties, and this fails.
        """
        corpus(tmp_path, a=100, b=200, c=300)

        outcome = prune_pre_migrate_corpus(
            {"output_dir": str(tmp_path), "retention_hours": 72}
        )

        assert outcome is not None
        assert names(outcome.exempted_by_minimum) == {"a.dump"}
        assert (tmp_path / "a.dump").exists()
        assert not (tmp_path / "c.dump").exists()

    def test_dry_run_selects_the_same_and_deletes_nothing(self, tmp_path: Path) -> None:
        corpus(tmp_path, a=100, b=200)
        config = {"output_dir": str(tmp_path), "retention_hours": 72}

        outcome = prune_pre_migrate_corpus(config, dry_run=True)

        assert outcome is not None
        assert names(outcome.removed) == {"b.dump"}
        assert (tmp_path / "b.dump").exists()


class TestTheGateUsesIt:
    """Pinned by patching the helper: the gate may not carry its own copy."""

    def test_the_dump_gate_prunes_through_the_helper(self, tmp_path: Path) -> None:
        config = {
            "enabled": True,
            "output_dir": str(tmp_path),
            "retention_hours": 72,
            "keep_last": 5,
        }
        strategy = MigrateStrategy(pre_migrate_dump=config, db_name="proddb")
        with (
            patch("fraisier.dbops.confiture.has_pending", return_value=True),
            patch(
                "fraisier.dbops.backup.run_backup",
                return_value=BackupResult(success=True, backup_path="x.dump"),
            ),
            patch("fraisier.dbops.backup.prune_pre_migrate_corpus") as prune,
        ):
            error = strategy._run_dump_gate(
                tmp_path / "c.yaml", migrations_dir=tmp_path, db_url=None
            )

        assert error is None
        prune.assert_called_once_with(config)

    def test_a_failed_dump_prunes_nothing(self, tmp_path: Path) -> None:
        """A failed dump never deletes anything."""
        config = {"enabled": True, "output_dir": str(tmp_path), "keep_last": 1}
        strategy = MigrateStrategy(pre_migrate_dump=config, db_name="proddb")
        with (
            patch("fraisier.dbops.confiture.has_pending", return_value=True),
            patch(
                "fraisier.dbops.backup.run_backup",
                return_value=BackupResult(success=False, error="boom"),
            ),
            patch("fraisier.dbops.backup.prune_pre_migrate_corpus") as prune,
        ):
            strategy._run_dump_gate(
                tmp_path / "c.yaml", migrations_dir=tmp_path, db_url=None
            )

        prune.assert_not_called()

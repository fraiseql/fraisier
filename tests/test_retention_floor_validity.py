"""`keep_minimum` protects the newest N *valid* dumps (#342).

`cleanup_old_backups` exempted by index into an mtime sort, newest first. A
dump still being written is the newest entry in the directory, so the floor's
first act was to protect the corrupt file — and if the producer had genuinely
stalled, to hold that slot while every valid dump aged out around it. "The
newest three are safe" reads like a validity guarantee and was not one:
`keep_minimum` counted, it did not validate.

The change has a precise limit, and most of these tests exist to pin it — and
to pin it accurately, because the tempting phrasing is wrong. "Prune never
deletes anything today's prune would have kept" is **false**: the invalid dump
itself was held by the floor and now is not, which is the entire point.

What holds is narrower and is the part an operator cares about: **no valid dump
is removed that the validity-blind floor would have kept**, and anything newly
removed is both unreadable and already past the retention cutoff. The floor
shifts by one, so a valid dump that would have aged out survives in the corrupt
file's place.

`UNVERIFIABLE` must spend slots normally. On a host with no `pg_restore` every
dump is unverifiable, and a floor that refused to spend slots on them would
exempt nothing — turning a missing binary into a corpus-wide retention change.
"""

import time
from pathlib import Path
from unittest.mock import patch

from fraisier.dbops.archive import ArchiveCheck, ArchiveVerdict
from fraisier.dbops.backup import cleanup_old_backups

_HOUR = 3600

# Corpus shapes exercised by the invariant tests below. `floor` is keep_minimum;
# values are ages in hours against a 48h cutoff, so under 48 is within retention.
_SHAPES: list[tuple[dict[str, float], int]] = [
    ({"x1.dump": 50, "x2.dump": 60, "x3.dump": 70}, 1),
    ({"y1.dump": 10, "y2.dump": 60, "y3.dump": 70}, 2),
    ({"z1.dump": 50, "z2.dump": 51, "z3.dump": 52, "z4.dump": 53}, 3),
    ({"w1.dump": 1, "w2.dump": 2}, 2),
]


def _dump(directory: Path, name: str, *, hours_old: float) -> Path:
    path = directory / name
    path.write_bytes(b"PGDMP" + b"\x00" * 32)
    stamp = time.time() - hours_old * _HOUR
    import os

    os.utime(path, (stamp, stamp))
    return path


def _verdicts(**by_name: ArchiveVerdict):
    """Patch `verify_archive` to answer per filename, defaulting to VALID.

    Patched on `fraisier.dbops.backup`, not on `fraisier.dbops.archive`:
    `backup` binds the name at import, so patching the defining module leaves
    its reference untouched and the real `pg_restore` runs — which rejects
    these stub files and makes every dump look INVALID.
    """

    def fake(path):
        verdict = by_name.get(Path(path).name, ArchiveVerdict.VALID)
        detail = "" if verdict is ArchiveVerdict.VALID else f"stubbed {verdict.value}"
        return ArchiveCheck(verdict, detail)

    return patch("fraisier.dbops.backup.verify_archive", side_effect=fake)


class TestFloorSkipsInvalidDumps:
    def test_slot_goes_to_the_valid_dump_not_the_newest(self, tmp_path: Path):
        """The defect, directly. Both past the cutoff; only one slot."""
        truncated = _dump(tmp_path, "newest_truncated.dump", hours_old=50)
        good = _dump(tmp_path, "older_good.dump", hours_old=60)

        with _verdicts(**{"newest_truncated.dump": ArchiveVerdict.INVALID}):
            outcome = cleanup_old_backups(
                tmp_path, retention_hours=48, keep_minimum=1, dry_run=True
            )

        assert str(good) in outcome.exempted_by_minimum
        assert str(truncated) not in outcome.exempted_by_minimum
        assert str(truncated) in outcome.removed

    def test_invalid_dump_inside_retention_is_still_kept(self, tmp_path: Path):
        """No new deletions. The age rule is untouched by validity."""
        fresh_bad = _dump(tmp_path, "fresh_truncated.dump", hours_old=1)

        with _verdicts(**{"fresh_truncated.dump": ArchiveVerdict.INVALID}):
            outcome = cleanup_old_backups(
                tmp_path, retention_hours=48, keep_minimum=1, dry_run=True
            )

        assert str(fresh_bad) in outcome.kept
        assert outcome.removed == ()

    def test_stalled_producer_keeps_a_real_dump_instead_of_the_corrupt_one(
        self, tmp_path: Path
    ):
        """The #339 shape: everything past the cutoff, newest one truncated."""
        _dump(tmp_path, "a_truncated.dump", hours_old=50)
        good_1 = _dump(tmp_path, "b_good.dump", hours_old=60)
        good_2 = _dump(tmp_path, "c_good.dump", hours_old=70)

        with _verdicts(**{"a_truncated.dump": ArchiveVerdict.INVALID}):
            outcome = cleanup_old_backups(
                tmp_path, retention_hours=48, keep_minimum=2, dry_run=True
            )

        assert set(outcome.exempted_by_minimum) == {str(good_1), str(good_2)}
        assert outcome.floor_was_load_bearing is True

    def _corpus(self, root: Path, label: str, ages: dict[str, float]) -> Path:
        directory = root / label
        directory.mkdir()
        for name, hours in ages.items():
            _dump(directory, name, hours_old=hours)
        return directory

    def test_all_valid_is_identical_to_the_validity_blind_algorithm(
        self, tmp_path: Path
    ):
        """The no-regression anchor: with nothing invalid, nothing changes.

        `slots` tracks the old `enumerate` index exactly when every candidate
        spends one, which is what keeps a corpus this cannot verify — every
        dump UNVERIFIABLE — retaining as it does today.
        """
        for index, (ages, floor) in enumerate(_SHAPES):
            directory = self._corpus(tmp_path, f"valid_{index}", ages)
            with _verdicts():
                all_valid = cleanup_old_backups(
                    directory, retention_hours=48, keep_minimum=floor, dry_run=True
                )
            with _verdicts(**dict.fromkeys(ages, ArchiveVerdict.UNVERIFIABLE)):
                unverifiable = cleanup_old_backups(
                    directory, retention_hours=48, keep_minimum=floor, dry_run=True
                )
            assert all_valid == unverifiable, f"{ages}, floor={floor}"

    def test_no_valid_dump_is_newly_deleted(self, tmp_path: Path):
        """The invariant that actually holds, stated exactly.

        Not "nothing today's prune would have kept": the invalid dump itself
        *was* held by the floor and now is not, which is the entire point. What
        is guaranteed is narrower and is the part an operator cares about — no
        **valid** dump is removed that the validity-blind floor would have kept.
        """
        for index, (ages, floor) in enumerate(_SHAPES):
            directory = self._corpus(tmp_path, f"invariant_{index}", ages)
            with _verdicts():
                blind = cleanup_old_backups(
                    directory, retention_hours=48, keep_minimum=floor, dry_run=True
                )
            newest = min(ages, key=lambda name: ages[name])
            for bad in (newest, *ages):
                with _verdicts(**{bad: ArchiveVerdict.INVALID}):
                    outcome = cleanup_old_backups(
                        directory, retention_hours=48, keep_minimum=floor, dry_run=True
                    )
                removed_valid = set(outcome.removed) - set(outcome.invalid)
                assert removed_valid <= set(blind.removed), (
                    f"a valid dump was newly deleted ({ages}, floor={floor}, bad={bad})"
                )

    def test_anything_newly_deleted_is_invalid_and_past_the_cutoff(
        self, tmp_path: Path
    ):
        """The other half: the only new removals are unreadable, expired dumps."""
        for index, (ages, floor) in enumerate(_SHAPES):
            directory = self._corpus(tmp_path, f"newly_{index}", ages)
            with _verdicts():
                blind = cleanup_old_backups(
                    directory, retention_hours=48, keep_minimum=floor, dry_run=True
                )
            for bad in ages:
                with _verdicts(**{bad: ArchiveVerdict.INVALID}):
                    outcome = cleanup_old_backups(
                        directory, retention_hours=48, keep_minimum=floor, dry_run=True
                    )
                for name in set(outcome.removed) - set(blind.removed):
                    assert name in outcome.invalid, f"{name} was not invalid"
                    assert ages[Path(name).name] > 48, f"{name} was within retention"


class TestUnverifiableSpendsSlotsNormally:
    def test_no_pg_restore_retains_exactly_as_before(self, tmp_path: Path):
        names = {"a.dump": 50, "b.dump": 60, "c.dump": 70}
        for name, hours in names.items():
            _dump(tmp_path, name, hours_old=hours)

        with _verdicts():
            baseline = cleanup_old_backups(
                tmp_path, retention_hours=48, keep_minimum=2, dry_run=True
            )
        with _verdicts(**dict.fromkeys(names, ArchiveVerdict.UNVERIFIABLE)):
            outcome = cleanup_old_backups(
                tmp_path, retention_hours=48, keep_minimum=2, dry_run=True
            )

        assert outcome.exempted_by_minimum == baseline.exempted_by_minimum
        assert outcome.removed == baseline.removed
        assert outcome.invalid == ()


class TestTheOverlayDoesNotBreakThePartition:
    """`invalid` names bad dumps wherever they landed. It is not a fourth group.

    `CleanupOutcome`'s three tuples partition the candidates and
    `floor_was_load_bearing` derives from that partition — the v0.59.0 trap.
    Adding a fourth member to the sum would break both.
    """

    def test_invalid_entries_also_appear_in_exactly_one_partition_member(
        self, tmp_path: Path
    ):
        _dump(tmp_path, "bad_old.dump", hours_old=50)
        _dump(tmp_path, "bad_older.dump", hours_old=55)
        _dump(tmp_path, "good.dump", hours_old=60)

        with _verdicts(
            **{
                "bad_old.dump": ArchiveVerdict.INVALID,
                "bad_older.dump": ArchiveVerdict.INVALID,
            }
        ):
            outcome = cleanup_old_backups(
                tmp_path, retention_hours=48, keep_minimum=1, dry_run=True
            )

        assert len(outcome.invalid) == 2
        for name in outcome.invalid:
            landed = [
                group
                for group in (
                    outcome.removed,
                    outcome.kept,
                    outcome.exempted_by_minimum,
                )
                if name in group
            ]
            assert len(landed) == 1, f"{name} landed in {len(landed)} groups"

    def test_partition_still_covers_every_candidate(self, tmp_path: Path):
        for index, hours in enumerate((1, 50, 60, 70)):
            _dump(tmp_path, f"d{index}.dump", hours_old=hours)

        with _verdicts(**{"d1.dump": ArchiveVerdict.INVALID}):
            outcome = cleanup_old_backups(
                tmp_path, retention_hours=48, keep_minimum=1, dry_run=True
            )

        total = (
            len(outcome.removed) + len(outcome.kept) + len(outcome.exempted_by_minimum)
        )
        assert total == 4


class TestPruneReportsWhatTheFloorExamined:
    """`invalid` is what allocating the floor found — not a corpus audit.

    A full sweep would shell out to `pg_restore --list` once per dump on every
    nightly run: ~1s each, so an hourly corpus retained for 30 days would spend
    minutes of a timer's life re-reading files the floor never consults. Prune
    verifies exactly the candidates that contest a slot, and `doctor` — which an
    operator invokes deliberately — is where the thorough sweep belongs.

    Stated here because the honest reading of `invalid` depends on it, and
    because a bounded scope that is not written down reads as a complete one.
    """

    def test_a_fresh_invalid_dump_is_not_verified_by_prune(self, tmp_path: Path):
        """It is kept by the age rule, so no slot is contested over it.

        Not a gap in coverage: the restore path verifies before it drops the
        database (#343), which is where a freshly arrived bad dump does harm.
        """
        _dump(tmp_path, "fresh_bad.dump", hours_old=1)

        with _verdicts(**{"fresh_bad.dump": ArchiveVerdict.INVALID}) as verify:
            outcome = cleanup_old_backups(
                tmp_path, retention_hours=48, keep_minimum=2, dry_run=True
            )

        verify.assert_not_called()
        assert outcome.invalid == ()

    def test_dumps_past_a_satisfied_floor_are_not_verified(self, tmp_path: Path):
        """Once the floor is full, validity cannot change any outcome."""
        _dump(tmp_path, "a.dump", hours_old=50)
        _dump(tmp_path, "b.dump", hours_old=60)
        _dump(tmp_path, "c.dump", hours_old=70)

        with _verdicts() as verify:
            cleanup_old_backups(
                tmp_path, retention_hours=48, keep_minimum=1, dry_run=True
            )

        checked = {Path(call.args[0]).name for call in verify.call_args_list}
        assert checked == {"a.dump"}


class TestPruneSaysItOutLoud:
    """A bad dump the floor refused must be visible, not merely accounted for.

    The warning goes to stderr beside the stalled-producer one, which is the
    channel the nightly timer's journal already carries — and the two together
    are the #339 state described completely: nothing recent is arriving *and*
    what did arrive cannot be read.
    """

    @staticmethod
    def _config(directory: Path, *, keep_minimum: int = 1):
        from unittest.mock import MagicMock

        from fraisier.config._validation import RetainEntry

        entry = RetainEntry(
            name="production-full",
            dir=str(directory),
            schedule="daily",
            match="*.dump",
            retention_days=2,
            keep_minimum=keep_minimum,
            user="postgres",
            environment="staging",
        )
        config = MagicMock()
        config.retain_entries.return_value = [entry]
        config.all_retain_entries.return_value = [entry]
        return config

    def _run(self, directory: Path, *, args=()):
        from click.testing import CliRunner

        from fraisier.cli.main import main

        with patch(
            "fraisier.cli.main.get_config", return_value=self._config(directory)
        ):
            return CliRunner().invoke(main, ["backup", "prune", "-e", "staging", *args])

    def test_an_invalid_dump_is_named_on_stderr(self, tmp_path: Path):
        _dump(tmp_path, "truncated.dump", hours_old=50)
        _dump(tmp_path, "good.dump", hours_old=60)

        with _verdicts(**{"truncated.dump": ArchiveVerdict.INVALID}):
            res = self._run(tmp_path, args=("--dry-run",))

        assert res.exit_code == 0, res.output
        assert "truncated.dump" in res.output
        # The deliberate warning, not `cleanup_old_backups`'s log line reaching
        # stderr through logging.lastResort — which an earlier basicConfig would
        # silently redirect, making this test pass for the wrong reason.
        assert "not readable archives" in res.output
        assert "keep_minimum slot" in res.output

    def test_a_clean_corpus_says_nothing_new(self, tmp_path: Path):
        _dump(tmp_path, "a.dump", hours_old=50)
        _dump(tmp_path, "b.dump", hours_old=60)

        with _verdicts():
            res = self._run(tmp_path, args=("--dry-run",))

        assert res.exit_code == 0, res.output
        assert "not a readable archive" not in res.output

    def test_json_report_carries_the_invalid_list(self, tmp_path: Path):
        import json as _json

        _dump(tmp_path, "truncated.dump", hours_old=50)
        _dump(tmp_path, "good.dump", hours_old=60)

        with _verdicts(**{"truncated.dump": ArchiveVerdict.INVALID}):
            res = self._run(tmp_path, args=("--dry-run", "--json"))

        assert res.exit_code == 0, res.output
        report = _json.loads(res.stdout)
        invalid = report["entries"][0]["invalid"]
        assert [Path(p).name for p in invalid] == ["truncated.dump"]

    def test_an_invalid_dump_does_not_fail_the_prune(self, tmp_path: Path):
        """A corrupt dump in the corpus is a warning, not a failed timer.

        Same reasoning as the disk threshold in #344: turning this into a
        non-zero exit stops the pruning that is the one thing still working.
        """
        _dump(tmp_path, "truncated.dump", hours_old=50)

        with _verdicts(**{"truncated.dump": ArchiveVerdict.INVALID}):
            res = self._run(tmp_path, args=("--dry-run",))

        assert res.exit_code == 0, res.output


class TestNoVerificationWhenTheFloorIsOff:
    def test_keep_minimum_zero_does_not_shell_out(self, tmp_path: Path):
        _dump(tmp_path, "a.dump", hours_old=50)
        _dump(tmp_path, "b.dump", hours_old=60)

        with patch("fraisier.dbops.archive.verify_archive") as verify:
            cleanup_old_backups(
                tmp_path, retention_hours=48, keep_minimum=0, dry_run=True
            )
        verify.assert_not_called()

    def test_nothing_past_the_cutoff_does_not_shell_out(self, tmp_path: Path):
        """Every dump is fresh, so no slot is ever contested."""
        _dump(tmp_path, "a.dump", hours_old=1)
        _dump(tmp_path, "b.dump", hours_old=2)

        with patch("fraisier.dbops.archive.verify_archive") as verify:
            cleanup_old_backups(
                tmp_path, retention_hours=48, keep_minimum=3, dry_run=True
            )
        verify.assert_not_called()

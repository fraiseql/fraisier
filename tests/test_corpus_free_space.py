"""Free space on a receiving host's corpus volumes (#344).

`check_disk_space` existed and was called from the backup command — on the host
that *produces* a dump. A host that *receives* a corpus by rsync had no guard,
and the #339 incident's first cause was `/backup/production` on the destination
growing until the disk filled.

v0.60.0 gave that host a retention policy, which bounds the corpus in the steady
state. Bounding is not alarming, and the distinction is the whole reason this
exists:

- the policy can be correct and the volume still fill, because something else
  on it grew;
- `keep_minimum` deliberately refuses to delete below a floor, so a stalled
  producer plus a full disk is a state retention will not resolve;
- #342 makes the floor prefer *valid* dumps, which is strictly better and still
  not a disk alarm.

`min_free_gb` is absent from every config written before this, and absent means
no threshold — so the default upgrade path gains a line of information and no
new failure.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import NamedTuple, TypeGuard
from unittest.mock import patch

import pytest
import yaml

from fraisier import doctor
from fraisier.config.loader import FraisierConfig

FRAISES_HEADER = """
project:
  name: my-project

scaffold:
  deploy_user: fraisier

fraises:
  api:
    type: api
    environments:
      development:
        app_path: /var/app/api
        git_repo: /srv/git/api.git
"""

_GB = 1024**3


def _config(tmp_path: Path, *entries: dict, env: str = "development"):
    block = yaml.safe_dump({"backup": {"environments": {env: {"retain": [*entries]}}}})
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(FRAISES_HEADER + "\n" + textwrap.dedent(block))
    return FraisierConfig(str(cfg))


class _Usage(NamedTuple):
    """What `shutil.disk_usage` returns, without reaching for its private type."""

    total: int
    used: int
    free: int


def _free(gb: float):
    """Patch `shutil.disk_usage` to report *gb* free, wherever it is asked."""
    usage = _Usage(total=100 * _GB, used=int((100 - gb) * _GB), free=int(gb * _GB))
    return patch("shutil.disk_usage", return_value=usage)


def _run(config) -> doctor.CheckResult:
    return doctor.DOCTOR_CHECKS["backup_corpus_free_space"].fn(config)


class TestRegistration:
    def test_the_check_is_registered(self):
        assert "backup_corpus_free_space" in doctor.DOCTOR_CHECKS

    def test_it_survives_a_none_config(self):
        result = _run(None)
        assert isinstance(result, doctor.CheckResult)
        assert result.status == "skip"


class TestThresholdOutcomes:
    def _entry(self, directory: Path, **overrides):
        entry = {
            "dir": str(directory),
            "retention_days": 3,
            "schedule": "*-*-* 05:30:00 UTC",
        }
        entry.update(overrides)
        return entry

    def test_below_threshold_fails_and_names_the_numbers(self, tmp_path: Path):
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        config = _config(tmp_path, self._entry(corpus, min_free_gb=20))

        with _free(5):
            result = _run(config)

        assert result.status == "fail"
        assert str(corpus) in result.detail
        assert "5" in result.detail
        assert "20" in result.detail

    def test_at_the_threshold_passes(self, tmp_path: Path):
        """`>=`, not `>` — a threshold names the acceptable floor."""
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        config = _config(tmp_path, self._entry(corpus, min_free_gb=20))

        with _free(20):
            result = _run(config)

        assert result.status == "pass"

    def test_above_threshold_passes_and_still_reports_free_space(self, tmp_path: Path):
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        config = _config(tmp_path, self._entry(corpus, min_free_gb=20))

        with _free(60):
            result = _run(config)

        assert result.status == "pass"
        assert "60" in result.detail

    def test_no_threshold_passes_and_says_none_is_set(self, tmp_path: Path):
        """The #341 philosophy: an absence is declared, not silent."""
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        config = _config(tmp_path, self._entry(corpus))

        with _free(1):
            result = _run(config)

        assert result.status == "pass"
        assert "no threshold" in result.detail.lower()
        assert str(corpus) in result.detail

    def test_a_missing_corpus_directory_fails(self, tmp_path: Path):
        """Already the #339 shape: a policy pointing at a path that is not there.

        `_prune_one` errors for the same reason — it prunes nothing, every
        night, reporting success.
        """
        config = _config(tmp_path, self._entry(tmp_path / "never_created"))

        result = _run(config)

        assert result.status == "fail"
        assert "never_created" in result.detail

    def test_no_retain_entries_skips(self, tmp_path: Path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(FRAISES_HEADER)
        result = _run(FraisierConfig(str(cfg)))
        assert result.status == "skip"

    def test_the_worst_entry_decides_the_status(self, tmp_path: Path):
        """One breached corpus fails the check even if another is fine."""
        good = tmp_path / "good"
        bad = tmp_path / "bad"
        good.mkdir()
        bad.mkdir()
        config = _config(
            tmp_path,
            self._entry(good, min_free_gb=1, name="good"),
            self._entry(bad, min_free_gb=90, name="bad"),
        )

        with _free(50):
            result = _run(config)

        assert result.status == "fail"
        assert "good" in result.detail
        assert "bad" in result.detail

    def test_an_unreadable_volume_is_a_skip_not_a_pass(self, tmp_path: Path):
        """ "I could not measure" must never read as "there is room".

        Same rule as `db restore`'s lock and #342's UNVERIFIABLE verdict: an
        unevaluable condition is not silently resolved in either direction.
        """
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        config = _config(tmp_path, self._entry(corpus, min_free_gb=20))

        with patch("shutil.disk_usage", side_effect=OSError("no such device")):
            result = _run(config)

        assert result.status == "skip"
        assert "no such device" in result.detail


class TestItReusesTheExistingPrimitive:
    def test_no_second_disk_usage_caller(self):
        """`free_space_gb` is the one place free space is measured.

        Caught a real one while this bundle was being written: the doctor check
        reached for `shutil.disk_usage` directly, which is how the producing and
        receiving sides came to disagree in the first place.
        """
        import ast

        import fraisier

        def _is_disk_usage(node: ast.AST) -> TypeGuard[ast.Attribute]:
            return (
                isinstance(node, ast.Attribute)
                and node.attr == "disk_usage"
                and isinstance(node.value, ast.Name)
                and node.value.id == "shutil"
            )

        package = Path(fraisier.__file__).parent
        offenders = [
            f"{path.relative_to(package)}:{node.lineno}"
            for path in sorted(package.rglob("*.py"))
            if path.name != "backup.py"
            for node in ast.walk(ast.parse(path.read_text()))
            if _is_disk_usage(node)
        ]

        assert not offenders, (
            "a second shutil.disk_usage call site (use dbops.backup."
            "free_space_gb):\n" + "\n".join(offenders)
        )


class TestPruneWarnsBelowThreshold:
    """The nightly timer says it, and still prunes.

    Failing the prune below the threshold would convert a disk warning into a
    failed unit and stop the pruning that is the one thing that might help.
    """

    @staticmethod
    def _prune_config(directory: Path, *, min_free_gb: int | None):
        from unittest.mock import MagicMock

        from fraisier.config.schema import RetainEntry

        entry = RetainEntry(
            environment="staging",
            name="production-full",
            dir=str(directory),
            schedule="daily",
            retention_days=2,
            match="*.dump",
            keep_minimum=1,
            user="postgres",
            min_free_gb=min_free_gb,
        )
        config = MagicMock()
        config.retain_entries.return_value = [entry]
        config.all_retain_entries.return_value = [entry]
        return config

    def _run_prune(self, directory: Path, *, min_free_gb: int | None):
        from click.testing import CliRunner

        from fraisier.cli.main import main
        from fraisier.dbops.archive import ArchiveCheck, ArchiveVerdict

        with (
            patch(
                "fraisier.cli.main.get_config",
                return_value=self._prune_config(directory, min_free_gb=min_free_gb),
            ),
            patch(
                "fraisier.dbops.backup.verify_archive",
                side_effect=lambda _p: ArchiveCheck(ArchiveVerdict.VALID, ""),
            ),
        ):
            return CliRunner().invoke(
                main, ["backup", "prune", "-e", "staging", "--dry-run"]
            )

    def test_below_threshold_warns_but_exits_zero(self, tmp_path: Path):
        (tmp_path / "a.dump").write_bytes(b"PGDMP")

        with _free(5):
            res = self._run_prune(tmp_path, min_free_gb=20)

        assert res.exit_code == 0, res.output
        assert "WARNING" in res.output
        assert "5" in res.output
        assert "20" in res.output

    def test_above_threshold_says_nothing(self, tmp_path: Path):
        (tmp_path / "a.dump").write_bytes(b"PGDMP")

        with _free(60):
            res = self._run_prune(tmp_path, min_free_gb=20)

        assert res.exit_code == 0, res.output
        assert "free" not in res.output.lower()

    def test_no_threshold_says_nothing(self, tmp_path: Path):
        (tmp_path / "a.dump").write_bytes(b"PGDMP")

        with _free(0.5):
            res = self._run_prune(tmp_path, min_free_gb=None)

        assert res.exit_code == 0, res.output
        assert "free" not in res.output.lower()

    def test_the_prune_still_happened(self, tmp_path: Path):
        """The warning must not become an early return."""
        import os
        import time

        doomed = tmp_path / "old.dump"
        doomed.write_bytes(b"PGDMP")
        stamp = time.time() - 100 * 3600
        os.utime(doomed, (stamp, stamp))

        with _free(1):
            res = self._run_prune(tmp_path, min_free_gb=20)

        assert res.exit_code == 0, res.output
        assert "old.dump" in res.output


@pytest.mark.parametrize("gb", [0.4, 19.9])
def test_fractional_free_space_below_an_integer_threshold_fails(
    tmp_path: Path, gb: float
):
    """19.9 GB free against a 20 GB floor is below it, not "about right"."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    config = _config(
        tmp_path,
        {
            "dir": str(corpus),
            "retention_days": 3,
            "schedule": "*-*-* 05:30:00 UTC",
            "min_free_gb": 20,
        },
    )

    with _free(gb):
        result = _run(config)

    assert result.status == "fail"

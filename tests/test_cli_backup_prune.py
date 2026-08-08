"""``fraisier backup prune`` — enforce a received corpus's retention (#339).

The command an operator runs by hand on the destination host, and the one
the rendered retention timer invokes. Its contract is deliberately loud:
the incident this closes is a story about work that did not happen
reporting success, so a typo'd directory is an error and a corpus kept
alive only by its floor is a warning.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from fraisier.cli.main import main

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


@pytest.fixture
def runner():
    return CliRunner()


def aged(path, *, hours: float):
    """Backdate *path* by *hours*, so the age rule has something to act on."""
    when = time.time() - hours * 3600
    os.utime(path, (when, when))
    return path


def corpus(tmp_path, name="production", *, dumps: dict[str, float]):
    """A directory of dumps, each mapped to its age in hours."""
    directory = tmp_path / name
    directory.mkdir()
    for filename, hours in dumps.items():
        path = directory / filename
        path.write_text("x")
        aged(path, hours=hours)
    return directory


def write_config(tmp_path, *entries: dict, env: str = "development"):
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        FRAISES_HEADER
        + "\n"
        + yaml.safe_dump(
            {"backup": {"environments": {env: {"retain": [*entries]}}}},
        )
    )
    return cfg


def entry(directory, **overrides) -> dict:
    return {
        "dir": str(directory),
        "retention_days": 1,
        "schedule": "*-*-* 05:30:00 UTC",
        **overrides,
    }


class TestPrune:
    def test_prune_applies_every_entry_for_the_environment(self, runner, tmp_path):
        """No --name means the whole environment's policy, not the first entry."""
        full = corpus(
            tmp_path, "full", dumps={"old.dump": 96, "new.dump": 1}
        )  # 4d and 1h
        slim = corpus(tmp_path, "slim", dumps={"stale.dump": 96, "fresh.dump": 1})
        cfg = write_config(
            tmp_path,
            entry(full, keep_minimum=0),
            entry(slim, keep_minimum=0),
        )

        result = runner.invoke(
            main, ["-c", str(cfg), "backup", "prune", "-e", "development"]
        )

        assert result.exit_code == 0, result.output
        assert not (full / "old.dump").exists()
        assert not (slim / "stale.dump").exists()
        assert (full / "new.dump").exists()
        assert (slim / "fresh.dump").exists()

    def test_prune_name_selects_one_entry(self, runner, tmp_path):
        full = corpus(tmp_path, "full", dumps={"old.dump": 96})
        slim = corpus(tmp_path, "slim", dumps={"stale.dump": 96})
        cfg = write_config(
            tmp_path,
            entry(full, keep_minimum=0),
            entry(slim, keep_minimum=0),
        )

        result = runner.invoke(
            main,
            ["-c", str(cfg), "backup", "prune", "-e", "development", "--name", "full"],
        )

        assert result.exit_code == 0, result.output
        assert not (full / "old.dump").exists()
        assert (slim / "stale.dump").exists(), "the unselected entry was pruned"

    def test_prune_unknown_name_exits_nonzero_listing_known_names(
        self, runner, tmp_path
    ):
        full = corpus(tmp_path, "full", dumps={"old.dump": 96})
        cfg = write_config(tmp_path, entry(full))

        result = runner.invoke(
            main,
            ["-c", str(cfg), "backup", "prune", "-e", "development", "--name", "nope"],
        )

        assert result.exit_code != 0
        assert "nope" in result.output
        assert "full" in result.output, "the known names are not listed"
        assert (full / "old.dump").exists(), "a failed selection still deleted"

    def test_prune_unknown_environment_exits_nonzero(self, runner, tmp_path):
        full = corpus(tmp_path, "full", dumps={"old.dump": 96})
        cfg = write_config(tmp_path, entry(full))

        result = runner.invoke(
            main, ["-c", str(cfg), "backup", "prune", "-e", "staging"]
        )

        assert result.exit_code != 0
        assert "staging" in result.output
        assert "development" in result.output

    def test_prune_missing_directory_is_an_error_not_a_silent_success(
        self, runner, tmp_path
    ):
        """A typo'd `dir` must not exit 0 having done nothing.

        The incident is a story about work that did not happen reporting
        success. A retention policy pointed at a directory that is not there
        is precisely that, and the timer would report OK nightly.
        """
        cfg = write_config(tmp_path, entry(tmp_path / "does-not-exist"))

        result = runner.invoke(
            main, ["-c", str(cfg), "backup", "prune", "-e", "development"]
        )

        assert result.exit_code != 0
        assert "does-not-exist" in result.output

    def test_prune_missing_directory_is_reported_for_every_entry(
        self, runner, tmp_path
    ):
        """One broken entry does not hide a second broken entry."""
        cfg = write_config(
            tmp_path,
            entry(tmp_path / "gone-a", name="a"),
            entry(tmp_path / "gone-b", name="b"),
        )

        result = runner.invoke(
            main, ["-c", str(cfg), "backup", "prune", "-e", "development"]
        )

        assert result.exit_code != 0
        assert "gone-a" in result.output
        assert "gone-b" in result.output

    def test_prune_dry_run_removes_nothing_and_lists_candidates(self, runner, tmp_path):
        full = corpus(tmp_path, "full", dumps={"old.dump": 96, "new.dump": 1})
        cfg = write_config(tmp_path, entry(full, keep_minimum=0))

        result = runner.invoke(
            main,
            ["-c", str(cfg), "backup", "prune", "-e", "development", "--dry-run"],
        )

        assert result.exit_code == 0, result.output
        assert (full / "old.dump").exists(), "--dry-run deleted a file"
        assert "old.dump" in result.output, "the candidate was not listed"

    def test_prune_warns_when_the_floor_was_the_only_thing_that_saved_the_corpus(
        self, runner, tmp_path
    ):
        """A stalled producer must be visible, and must not break the timer.

        Every survivor is past the cutoff: nothing arrived recently. Exit 0,
        because a non-zero here would put the timer in `failed` and stop the
        pruning that is still working.
        """
        full = corpus(
            tmp_path, "full", dumps={"a.dump": 96, "b.dump": 120, "c.dump": 144}
        )
        cfg = write_config(tmp_path, entry(full, keep_minimum=3))

        result = runner.invoke(
            main, ["-c", str(cfg), "backup", "prune", "-e", "development"]
        )

        assert result.exit_code == 0, result.output
        assert "WARNING" in result.stderr
        assert str(full) in result.stderr
        assert "96h" in result.stderr, "the newest dump's age is not named"
        assert all((full / f).exists() for f in ("a.dump", "b.dump", "c.dump"))

    def test_no_warning_when_a_survivor_is_within_retention(self, runner, tmp_path):
        full = corpus(tmp_path, "full", dumps={"a.dump": 96, "fresh.dump": 1})
        cfg = write_config(tmp_path, entry(full, keep_minimum=3))

        result = runner.invoke(
            main, ["-c", str(cfg), "backup", "prune", "-e", "development"]
        )

        assert result.exit_code == 0, result.output
        assert "WARNING" not in result.stderr

    def test_prune_json_reports_removed_kept_and_stale(self, runner, tmp_path):
        """The three groups partition the corpus, and the JSON keeps them apart.

        `recent` is within retention and would survive with no floor at all.
        `mid` is past the cutoff and survives only because it sits inside the
        exemption slice. Collapsing the two into "kept" is what would make
        `floor_was_load_bearing` unknowable downstream.
        """
        full = corpus(
            tmp_path,
            "full",
            dumps={"recent.dump": 1, "mid.dump": 96, "oldest.dump": 120},
        )
        cfg = write_config(tmp_path, entry(full, keep_minimum=2))

        result = runner.invoke(
            main, ["-c", str(cfg), "backup", "prune", "-e", "development", "--json"]
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        (report,) = payload["entries"]
        assert report["name"] == "full"
        assert report["dir"] == str(full)
        assert [Path(p).name for p in report["removed"]] == ["oldest.dump"]
        assert [Path(p).name for p in report["kept"]] == ["recent.dump"]
        assert [Path(p).name for p in report["exempted_by_minimum"]] == ["mid.dump"]
        assert report["floor_was_load_bearing"] is False

    def test_prune_json_is_the_only_thing_on_stdout(self, runner, tmp_path):
        """`--json` output is piped; anything in front of it is a parse error.

        This corpus also trips the stalled-producer warning, so it pins the
        split: the report goes to stdout, the warning to stderr, and
        `json.loads(stdout)` still works.
        """
        full = corpus(tmp_path, "full", dumps={"a.dump": 96, "b.dump": 120})
        cfg = write_config(tmp_path, entry(full, keep_minimum=2))

        result = runner.invoke(
            main, ["-c", str(cfg), "backup", "prune", "-e", "development", "--json"]
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["entries"][0]["floor_was_load_bearing"] is True
        assert "WARNING" in result.stderr

    def test_prune_match_scopes_the_glob(self, runner, tmp_path):
        full = corpus(
            tmp_path,
            "full",
            dumps={"db_full_1.dump": 96, "db_slim_1.dump": 96},
        )
        cfg = write_config(tmp_path, entry(full, match="*_full_*.dump", keep_minimum=0))

        result = runner.invoke(
            main, ["-c", str(cfg), "backup", "prune", "-e", "development"]
        )

        assert result.exit_code == 0, result.output
        assert not (full / "db_full_1.dump").exists()
        assert (full / "db_slim_1.dump").exists(), "match did not scope the glob"

    def test_prune_with_no_policy_for_the_environment_says_so(self, runner, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(FRAISES_HEADER)

        result = runner.invoke(
            main, ["-c", str(cfg), "backup", "prune", "-e", "development"]
        )

        assert result.exit_code != 0
        assert "no retention" in result.output.lower()


class TestBackupGroupBackCompat:
    """`fraisier backup <fraise> -e <env>` predates `fraisier backup prune`.

    Turning `backup` into a group must not break the documented form.
    """

    def test_the_legacy_positional_form_still_reaches_run(self, runner, tmp_path):
        from unittest.mock import MagicMock, patch

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(FRAISES_HEADER)

        with (
            patch("fraisier.dbops.guard.is_external_db", return_value=False),
            patch("fraisier.dbops.backup.check_disk_space", return_value=True),
            patch(
                "fraisier.dbops.backup.run_backup",
                return_value=MagicMock(success=True, backup_path="/backup/x.dump"),
            ) as run,
        ):
            result = runner.invoke(
                main, ["-c", str(cfg), "backup", "api", "-e", "development"]
            )

        assert result.exit_code != 2, f"argument parsing broke:\n{result.output}"
        assert run.called or "database_url" in result.output

    def test_backup_help_lists_prune(self, runner):
        result = runner.invoke(main, ["backup", "--help"])
        assert result.exit_code == 0
        assert "prune" in result.output

"""Which files does this ship touch, and can we tell? (#346)

`ship` decided whether a `triggers:` check ran by diffing the **working tree**.
A changeset that is already committed leaves a clean tree, so the changed-file
list was empty and *every* triggered check was skipped — silently, because
`_run_phase` filtered them out before the only code that prints. The reporter
had 12 triggered checks and a changeset touching 6 files under `db/`; four ran,
being exactly the four with no `triggers:` at all. One of the silent ones was
the gate blocking a schema change with no migration.

These tests drive **real git repositories** rather than mocking
`subprocess.run`. The defect is *which git command runs*, so a mock asserting
"we invoked `git diff HEAD`" would have passed happily against the broken code.
The pre-existing tests patch `_get_changed_files` wholesale, which is precisely
why neither the empty-list path nor the command itself was ever covered.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from fraisier.ship.pipeline import ShipPipeline

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _write(repo: Path, rel: str, text: str = "x\n") -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with a `main` base branch and a `feature` branch checked out.

    Modelled on the reporter's situation: an `origin` remote exists (a bare
    clone on disk, so nothing touches the network) and `origin/main` is the PR
    target.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    _git(work, "config", "commit.gpgsign", "false")
    _write(work, "README.md", "base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-u", "origin", "main")
    _git(work, "checkout", "-b", "feature")
    return work


def _pipeline(repo: Path, *, pr_base: str | None = None, checks=()) -> ShipPipeline:
    import io

    from rich.console import Console

    from fraisier.config import ShipConfig

    return ShipPipeline(
        config=ShipConfig(checks=list(checks), pr_base=pr_base),
        cwd=repo,
        console=Console(file=io.StringIO()),
    )


def _scope(repo: Path, **kwargs):
    return _pipeline(repo, **kwargs).trigger_scope()


class TestCommittedChangesCount:
    """The reported defect, driven end to end."""

    def test_a_committed_change_on_a_clean_tree_is_seen(self, repo: Path):
        _write(repo, "db/migrations/001.sql", "select 1;\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "add migration")
        assert _git(repo, "status", "--porcelain") == "", "tree should be clean"

        scope = _scope(repo, pr_base="main")

        assert scope.undetermined is False
        assert "db/migrations/001.sql" in scope.files

    def test_several_committed_files_are_all_seen(self, repo: Path):
        for name in ("db/a.sql", "db/b.sql", "src/main.py"):
            _write(repo, name)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "six files")

        scope = _scope(repo, pr_base="main")

        assert scope.files == frozenset({"db/a.sql", "db/b.sql", "src/main.py"})


class TestTheSetIsAUnion:
    """Checks run before `git add --update`, so uncommitted work still counts."""

    def test_working_tree_only_changes_are_seen(self, repo: Path):
        _write(repo, "README.md", "edited\n")

        scope = _scope(repo, pr_base="main")

        assert scope.undetermined is False
        assert "README.md" in scope.files

    def test_committed_and_uncommitted_are_unioned(self, repo: Path):
        _write(repo, "db/migrations/001.sql", "select 1;\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "committed")
        _write(repo, "README.md", "edited too\n")

        scope = _scope(repo, pr_base="main")

        assert scope.files == frozenset({"db/migrations/001.sql", "README.md"})

    def test_a_staged_but_uncommitted_change_is_seen(self, repo: Path):
        _write(repo, "src/new.py")
        _git(repo, "add", "-A")

        scope = _scope(repo, pr_base="main")

        assert "src/new.py" in scope.files

    def test_a_file_in_both_halves_appears_once(self, repo: Path):
        _write(repo, "db/a.sql", "one\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "committed")
        _write(repo, "db/a.sql", "one then two\n")

        scope = _scope(repo, pr_base="main")

        assert scope.files == frozenset({"db/a.sql"})


class TestEmptyIsNotUndetermined:
    """A ship that touches nothing is a real, knowable answer."""

    def test_no_changes_at_all_is_determined_and_empty(self, repo: Path):
        scope = _scope(repo, pr_base="main")

        assert scope.undetermined is False
        assert scope.files == frozenset()

    def test_an_unresolvable_merge_base_is_undetermined(self, repo: Path):
        """Not empty. `[]` was the old answer and it silently skipped."""
        scope = _scope(repo, pr_base="a-branch-that-does-not-exist")

        assert scope.undetermined is True
        assert scope.files is None

    def test_a_tree_that_is_not_a_repo_is_undetermined(self, tmp_path: Path):
        not_a_repo = tmp_path / "bare"
        not_a_repo.mkdir()

        scope = _scope(not_a_repo)

        assert scope.undetermined is True


class TestBaseResolutionOrder:
    def test_explicit_pr_base_is_used(self, repo: Path):
        _write(repo, "db/a.sql")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "c")

        scope = _scope(repo, pr_base="main")

        assert scope.base == "origin/main"

    def test_origin_head_is_the_fallback(self, repo: Path):
        """No `ship.pr_base`: a local ref lookup still resolves the base.

        Falling straight to "run everything" here would make every repo without
        `ship.pr_base` run every triggered check on every ship.
        """
        _git(repo, "remote", "set-head", "origin", "main")
        _write(repo, "db/a.sql")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "c")

        scope = _scope(repo)

        assert scope.undetermined is False
        assert scope.base == "origin/main"
        assert "db/a.sql" in scope.files

    def test_no_base_and_no_origin_head_is_undetermined(self, repo: Path):
        _git(repo, "remote", "remove", "origin")

        scope = _scope(repo)

        assert scope.undetermined is True
        assert scope.detail

    def test_the_current_branch_is_never_the_base(self, repo: Path):
        """The trap in the obvious fallback.

        `_assert_no_version_race` resolves `pr_base or current_branch`, which is
        right for a version race. Here it is fatal: on a pushed feature branch
        `merge-base(HEAD, origin/<current-branch>)` is HEAD, so the diff is
        empty and the original bug is reproduced by the fallback meant to fix
        it.
        """
        _write(repo, "db/a.sql")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "c")
        _git(repo, "push", "-u", "origin", "feature")
        _git(repo, "remote", "set-head", "origin", "main")

        scope = _scope(repo)

        assert scope.base != "origin/feature"
        assert "db/a.sql" in scope.files, (
            "resolved against the current branch: merge-base is HEAD, so the "
            "committed change vanished — this is #346 reproduced"
        )


class TestTheCliBaseReachesTriggerEvaluation:
    """`--pr-base` was invisible to triggers before #346.

    `ShipPipeline` was built from `ShipConfig` alone, and the CLI's resolved
    base only ever reached the version-race check — and only when `--pr` was
    passed. So `ship --pr-base dev` evaluated triggers against a different base
    than the PR it was about to open, which is this issue again in a new place.
    """

    def test_explicit_argument_overrides_the_config(self, repo: Path):
        pipeline = _pipeline(repo, pr_base="main")
        overridden = ShipPipeline(
            config=pipeline._config,
            cwd=repo,
            console=pipeline._console,
            pr_base="other",
        )
        assert overridden._pr_base == "other"

    def test_config_is_the_fallback(self, repo: Path):
        assert _pipeline(repo, pr_base="main")._pr_base == "main"

    def test_ship_passes_the_resolved_base_not_the_race_base(self):
        """The race base is None without `--pr`; the trigger base must not be.

        A non-PR ship has no version race to lose but still has triggered
        checks to select, so reusing `race_base` here would switch triggers off
        for every `ship` without `--pr`.
        """
        import inspect

        from fraisier.cli import version as version_mod

        source = inspect.getsource(version_mod._ship_commit_push_deploy)
        assert "trigger_base=resolved_pr_base" in source, (
            "triggers must be evaluated against the resolved base, not race_base"
        )


class TestComputedOncePerRun:
    def test_three_triggered_checks_resolve_the_scope_once(self, repo: Path):
        from unittest.mock import patch

        from fraisier.config import ShipCheckConfig

        checks = [
            ShipCheckConfig(
                name=f"c{i}", command=["true"], phase="validate", triggers=["db/**"]
            )
            for i in range(3)
        ]
        pipeline = _pipeline(repo, pr_base="main", checks=checks)

        with patch.object(
            ShipPipeline,
            "_compute_trigger_scope",
            wraps=pipeline._compute_trigger_scope,
        ) as compute:
            pipeline.run_verify_phase()

        assert compute.call_count == 1, (
            f"resolved the changed set {compute.call_count} times for 3 checks"
        )

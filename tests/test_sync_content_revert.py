"""Source-side content reverts must survive the pre-merge (#290, second half).

`f9388b9` fixed the deletion half and said the content-revert half needed its
own fix. This is that case.

When source reverts a file to **exactly** its merge-base content, git's 3-way
merge sees ``ours == base`` and resolves it as *take theirs* — silently, with a
zero exit code and no conflict. Given a correct base that is right. Under squash
promotion the base is the ancient fork point that never advances, so "ours ==
base" no longer means "source never touched this"; it means "source added it and
then reverted it", and target's stale promoted copy wins.

A revert to any *other* content conflicts instead, and tier 3 already handles it.
That asymmetry is why this half survived the first fix.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from fraisier.cli.sync import (
    _propagate_source_reverts,
    _target_blob_is_source_derived,
)

BASE_SHARED = "registry = []\n"
PROMOTED_SHARED = "registry = ['feature']\n"
TARGET_HOTFIX = "x = 99  # staging-only hotfix\n"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """`dev` and `staging` related only by squash promotion, with a reverted file.

    Three subjects, chosen so that the only difference between the file that
    must be restored and the file that must not is gate 3:

    * ``shared.py``   — dev added wiring, promoted it, then reverted it exactly.
    * ``untouched.py``— staging authored its own hotfix; dev never touched it
      again. Same merge shape as ``shared.py`` (``ours == base``, merge takes
      theirs) so only "is target's blob source-derived?" separates them.
    * ``partial.py``  — promoted, then diverged on both sides by
      ``_diverge_partial_on_dev`` to produce a genuine conflict.

    The fixture chdirs into the work tree: the code under test shells out to git
    against the process cwd, so a test that forgot would silently run against
    the fraisier repo.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "dev", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "T", cwd=work)

    # --- the ancient common ancestor -------------------------------------
    (work / "README.md").write_text("base\n")
    (work / "shared.py").write_text(BASE_SHARED)
    (work / "untouched.py").write_text("x = 1\n")
    (work / "partial.py").write_text("v = 0\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "base", cwd=work)
    _git("branch", "staging", cwd=work)

    # --- dev adds the wiring, well after the fork ------------------------
    (work / "shared.py").write_text(PROMOTED_SHARED)
    (work / "partial.py").write_text("v = 1\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "add wiring", cwd=work)

    # --- promotion: staging receives dev's tree as ONE squashed commit ---
    _git("checkout", "-q", "staging", cwd=work)
    _git("checkout", "dev", "--", ".", cwd=work)
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "Promote dev -> staging", cwd=work)

    # --- staging authors its own change, and a target-only artifact ------
    (work / "untouched.py").write_text(TARGET_HOTFIX)
    (work / "RELEASE_NOTES.md").write_text("v1\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "staging hotfix + release notes", cwd=work)

    # --- dev reverts the wiring back to exactly the base content ---------
    _git("checkout", "-q", "dev", cwd=work)
    (work / "shared.py").write_text(BASE_SHARED)
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "revert wiring", cwd=work)

    # --- publish as `origin/*` so the code under test can address them ---
    bare = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(bare), cwd=work)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "-q", "origin", "dev", "staging", cwd=work)
    _git("fetch", "-q", "origin", cwd=work)
    monkeypatch.chdir(work)
    return work


def _diverge_partial_on_dev(work: Path) -> None:
    """Move dev's partial.py off both base and the promoted copy — a real conflict."""
    _git("checkout", "-q", "dev", cwd=work)
    (work / "partial.py").write_text("v = 2\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "diverge partial", cwd=work)
    _git("push", "-q", "origin", "dev", cwd=work)
    _git("fetch", "-q", "origin", cwd=work)


def _premerge(work: Path) -> subprocess.CompletedProcess:
    """Reproduce sync's pre-merge: branch from dev, merge staging in."""
    _git("checkout", "-q", "-B", "syncbranch", "origin/dev", cwd=work)
    return subprocess.run(
        ["git", "merge", "origin/staging", "--no-edit", "--no-commit"],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )


def _index_blob(work: Path, path: str) -> str:
    return _git("rev-parse", f":{path}", cwd=work).stdout.strip()


def _rev(work: Path, ref: str) -> str:
    return _git("rev-parse", ref, cwd=work).stdout.strip()


class TestTheSilentResurrection:
    """The bug: git takes target's stale copy without saying anything."""

    def test_premerge_reports_no_conflict_at_all(self, repo):
        """Precondition — this is why the tier loop never sees it."""
        result = _premerge(repo)

        assert result.returncode == 0
        assert not _git(
            "diff", "--name-only", "--diff-filter=U", cwd=repo
        ).stdout.strip()

    def test_revert_is_lost_before_the_pass_runs(self, repo):
        """Precondition — the merge alone resurrects the reverted wiring."""
        _premerge(repo)

        assert (repo / "shared.py").read_text() == PROMOTED_SHARED

    def test_reverted_file_is_reported_as_restored(self, repo):
        _premerge(repo)

        assert _propagate_source_reverts("dev", "staging") == ["shared.py"]

    def test_worktree_holds_the_reverted_content(self, repo):
        _premerge(repo)
        _propagate_source_reverts("dev", "staging")

        assert (repo / "shared.py").read_text() == BASE_SHARED

    def test_index_holds_the_reverted_content(self, repo):
        """Reporting the path is not enough — the index is what gets committed."""
        _premerge(repo)
        _propagate_source_reverts("dev", "staging")

        assert _index_blob(repo, "shared.py") == _rev(repo, "origin/dev:shared.py")


class TestTargetAuthoredContentIsLeftAlone:
    """The highest-consequence regression: never clobber target's own work.

    ``untouched.py`` has the *same merge shape* as the restored file — dev's copy
    equals the base, so the merge takes theirs. Only gate 3 tells them apart.
    """

    def test_target_hotfix_is_not_claimed(self, repo):
        _premerge(repo)

        assert "untouched.py" not in _propagate_source_reverts("dev", "staging")

    def test_target_hotfix_content_survives(self, repo):
        _premerge(repo)
        _propagate_source_reverts("dev", "staging")

        assert (repo / "untouched.py").read_text() == TARGET_HOTFIX

    def test_gate_three_is_what_makes_that_safe(self, repo):
        """The blob staging authored never appears in dev's history of the path."""
        _premerge(repo)

        assert _target_blob_is_source_derived("dev", "staging", "untouched.py") is False
        assert _target_blob_is_source_derived("dev", "staging", "shared.py") is True

    def test_target_only_file_is_not_a_candidate(self, repo):
        """A file source never had is absent, not modified — never in scope."""
        _premerge(repo)

        assert "RELEASE_NOTES.md" not in _propagate_source_reverts("dev", "staging")


class TestConflictsAreLeftToTheTierLoop:
    """A path with conflict markers has no stage-0 entry, so it excludes itself."""

    def test_conflicted_path_is_not_claimed(self, repo):
        _diverge_partial_on_dev(repo)
        assert _premerge(repo).returncode != 0

        assert "partial.py" not in _propagate_source_reverts("dev", "staging")

    def test_conflicted_path_stays_unmerged_for_tier_three(self, repo):
        _diverge_partial_on_dev(repo)
        _premerge(repo)
        _propagate_source_reverts("dev", "staging")

        unmerged = _git(
            "diff", "--name-only", "--diff-filter=U", cwd=repo
        ).stdout.split()
        assert "partial.py" in unmerged

    def test_the_silent_path_is_still_restored_alongside_a_conflict(self, repo):
        """The pass must run on conflicted merges too, not only clean ones."""
        _diverge_partial_on_dev(repo)
        _premerge(repo)

        assert _propagate_source_reverts("dev", "staging") == ["shared.py"]


class TestNothingToDo:
    """No revert, no candidates — the pass must be inert."""

    def test_clean_promotion_restores_nothing(self, repo, monkeypatch):
        """With dev's revert undone, dev and staging agree on shared.py."""
        _git("checkout", "-q", "dev", cwd=repo)
        (repo / "shared.py").write_text(PROMOTED_SHARED)
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "un-revert", cwd=repo)
        _git("push", "-q", "origin", "dev", cwd=repo)
        _git("fetch", "-q", "origin", cwd=repo)
        _premerge(repo)

        assert _propagate_source_reverts("dev", "staging") == []

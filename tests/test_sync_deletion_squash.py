"""Source-side deletions must propagate under squash-merge promotion (#290).

`fraisier sync` squash-merges its own PRs, so `git merge-base origin/<source>
origin/<tgt>` never advances past the original fork point. Anchoring deletion
detection on it meant a file created on source *after* that base and later
deleted there was invisible — while target still carried a copy from an earlier
squash-sync. The file resurrected on every sync.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from fraisier.cli.sync import (
    _propagate_source_deletions,
    _source_deleted_path,
    _target_blob_is_source_derived,
)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A repo with `dev` and `staging` related only by squash promotion.

    Deliberately mirrors what fraisier's own sync produces: `staging` receives
    dev's content as a single squashed commit whose parent is the previous
    staging tip, recording no link to dev's history.

    The fixture chdirs into the work tree: every helper under test shells out
    to git against the process cwd, so a test that forgot to chdir would
    silently run against the fraisier repo and pass or fail for the wrong
    reason.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "dev", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "T", cwd=work)

    # --- the ancient common ancestor -------------------------------------
    (work / "README.md").write_text("base\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "base", cwd=work)
    _git("branch", "staging", cwd=work)

    # --- dev adds a feature, well after the fork -------------------------
    (work / "feature.py").write_text("def go():\n    return 1\n")
    (work / "shared.py").write_text("registry = ['feature']\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "add feature", cwd=work)

    # --- promotion: staging receives dev's tree as ONE squashed commit ---
    _git("checkout", "-q", "staging", cwd=work)
    _git("checkout", "dev", "--", ".", cwd=work)
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "Promote dev -> staging", cwd=work)

    # --- dev iterates, then deletes the feature --------------------------
    _git("checkout", "-q", "dev", cwd=work)
    (work / "feature.py").write_text("def go():\n    return 2\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "tweak feature", cwd=work)

    # a target-owned artifact dev never sees
    _git("checkout", "-q", "staging", cwd=work)
    (work / "RELEASE_NOTES.md").write_text("v1\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "release notes", cwd=work)
    _git("checkout", "-q", "dev", cwd=work)

    # --- publish as `origin/*` so the code under test can address them ---
    bare = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(bare), cwd=work)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "-q", "origin", "dev", "staging", cwd=work)
    _git("fetch", "-q", "origin", cwd=work)
    monkeypatch.chdir(work)
    return work


def _delete_feature_on_dev(work: Path) -> None:
    _git("checkout", "-q", "dev", cwd=work)
    (work / "feature.py").unlink()
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "drop feature", cwd=work)
    _git("push", "-q", "origin", "dev", cwd=work)
    _git("fetch", "-q", "origin", cwd=work)


def _premerge(work: Path) -> None:
    """Reproduce sync's pre-merge: branch from dev, merge staging in."""
    _git("checkout", "-q", "-B", "syncbranch", "origin/dev", cwd=work)
    subprocess.run(
        ["git", "merge", "origin/staging", "--no-edit", "--no-commit"],
        cwd=work,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_propagate(work: Path, monkeypatch) -> list[str]:
    """Run the pre-pass. The `repo` fixture has already chdir'ed us in."""
    return _propagate_source_deletions("dev", "staging")


class TestSquashTopologyMiss:
    """The bug: a stale merge-base hides the deletion entirely."""

    def test_merge_base_is_stale_by_construction(self, repo):
        """Precondition — the merge-base is the ancient fork, not the promotion."""
        _delete_feature_on_dev(repo)
        base = _git(
            "merge-base", "origin/dev", "origin/staging", cwd=repo
        ).stdout.strip()
        first = _git("rev-list", "--max-parents=0", "origin/dev", cwd=repo).stdout
        assert base == first.split()[0], "merge-base should be the original fork"

    def test_deletion_is_propagated(self, repo, monkeypatch):
        """The file dev deleted is removed from the sync branch."""
        _delete_feature_on_dev(repo)
        _premerge(repo)

        assert _run_propagate(repo, monkeypatch) == ["feature.py"]

    def test_file_is_actually_gone_from_the_index(self, repo, monkeypatch):
        """Returning the path is not enough — the index must reflect it."""
        _delete_feature_on_dev(repo)
        _premerge(repo)
        _run_propagate(repo, monkeypatch)

        tracked = _git("ls-files", cwd=repo).stdout.split()
        assert "feature.py" not in tracked


class TestTargetOwnedFilesAreSafe:
    """The highest-consequence regression: never delete target's own files."""

    def test_target_only_file_untouched(self, repo, monkeypatch):
        """A file source never had is not a deletion candidate."""
        _delete_feature_on_dev(repo)
        _premerge(repo)

        assert "RELEASE_NOTES.md" not in _run_propagate(repo, monkeypatch)

    def test_target_only_file_still_tracked(self, repo, monkeypatch):
        """And it is still in the index afterwards."""
        _delete_feature_on_dev(repo)
        _premerge(repo)
        _run_propagate(repo, monkeypatch)

        assert "RELEASE_NOTES.md" in _git("ls-files", cwd=repo).stdout.split()

    def test_gate_one_rejects_a_path_source_never_had(self, repo):
        """_source_deleted_path is what makes that safe."""
        _delete_feature_on_dev(repo)

        assert _source_deleted_path("dev", "RELEASE_NOTES.md") is False
        assert _source_deleted_path("dev", "feature.py") is True


class TestSourceEditedThenDeleted:
    """The case a merge-base/pre-deletion-blob comparison gets wrong.

    Target's copy is source's content *as of the last promotion*. Source then
    edited the file before deleting it, so target's blob differs from the
    pre-deletion blob — but it is still source-derived and must propagate.
    """

    def test_edited_then_deleted_still_propagates(self, repo, monkeypatch):
        """dev tweaked feature.py after promotion, then deleted it."""
        _delete_feature_on_dev(repo)
        _premerge(repo)

        assert "feature.py" in _run_propagate(repo, monkeypatch)

    def test_blob_gate_recognises_the_promoted_version(self, repo):
        """staging's blob predates dev's tweak but is still source-derived."""
        _delete_feature_on_dev(repo)

        assert _target_blob_is_source_derived("dev", "staging", "feature.py") is True


class TestTargetAuthoredContentIsLeftAlone:
    """If target wrote its own version, the operator decides."""

    def test_target_edit_blocks_propagation(self, repo, monkeypatch):
        """staging edits feature.py itself; dev deletes it → leave it."""
        _git("checkout", "-q", "staging", cwd=repo)
        (repo / "feature.py").write_text("def go():\n    return 'staging-local'\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "staging-local change", cwd=repo)
        _git("push", "-q", "origin", "staging", cwd=repo)
        _git("fetch", "-q", "origin", cwd=repo)

        _delete_feature_on_dev(repo)
        _premerge(repo)

        assert "feature.py" not in _run_propagate(repo, monkeypatch)

    def test_blob_gate_rejects_target_authored_content(self, repo):
        """_target_blob_is_source_derived is what blocks it."""
        _git("checkout", "-q", "staging", cwd=repo)
        (repo / "feature.py").write_text("def go():\n    return 'staging-local'\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "staging-local change", cwd=repo)
        _git("push", "-q", "origin", "staging", cwd=repo)
        _git("fetch", "-q", "origin", cwd=repo)
        _delete_feature_on_dev(repo)

        assert _target_blob_is_source_derived("dev", "staging", "feature.py") is False


class TestRenames:
    """git diff applies rename detection by default; --no-renames is required."""

    def test_rename_on_source_propagates_the_old_path(self, repo, monkeypatch):
        """dev renames feature.py → feature2.py; the old path must go."""
        _git("checkout", "-q", "dev", cwd=repo)
        _git("mv", "feature.py", "feature2.py", cwd=repo)
        _git("commit", "-qm", "rename feature", cwd=repo)
        _git("push", "-q", "origin", "dev", cwd=repo)
        _git("fetch", "-q", "origin", cwd=repo)
        _premerge(repo)

        assert "feature.py" in _run_propagate(repo, monkeypatch)

    def test_renamed_new_path_is_present(self, repo, monkeypatch):
        """The new path arrives via the merge, so nothing is lost."""
        _git("checkout", "-q", "dev", cwd=repo)
        _git("mv", "feature.py", "feature2.py", cwd=repo)
        _git("commit", "-qm", "rename feature", cwd=repo)
        _git("push", "-q", "origin", "dev", cwd=repo)
        _git("fetch", "-q", "origin", cwd=repo)
        _premerge(repo)
        _run_propagate(repo, monkeypatch)

        assert "feature2.py" in _git("ls-files", cwd=repo).stdout.split()


class TestAwkwardPaths:
    """NUL-delimited output, so quoting and odd characters do not break it."""

    def test_path_with_spaces_and_non_ascii(self, repo, monkeypatch):
        """A deleted path with a space and an accent still propagates."""
        odd = "dir with spaces/fichier é.py"
        _git("checkout", "-q", "dev", cwd=repo)
        (repo / "dir with spaces").mkdir()
        (repo / odd).write_text("x = 1\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "add odd path", cwd=repo)

        _git("checkout", "-q", "staging", cwd=repo)
        _git("checkout", "dev", "--", ".", cwd=repo)
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "promote odd path", cwd=repo)

        _git("checkout", "-q", "dev", cwd=repo)
        (repo / odd).unlink()
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "drop odd path", cwd=repo)
        _git("push", "-q", "origin", "dev", "staging", cwd=repo)
        _git("fetch", "-q", "origin", cwd=repo)
        _premerge(repo)

        assert odd in _run_propagate(repo, monkeypatch)


class TestFailurePosture:
    """A git error must never authorise a deletion."""

    def test_blob_gate_returns_false_on_unknown_path(self, repo):
        """rev-parse failure → False, not a crash and not a True."""
        assert _target_blob_is_source_derived("dev", "staging", "nope.py") is False

    def test_deleted_gate_returns_false_on_unknown_ref(self, repo):
        """A bad ref is a False, so the caller skips the path."""
        assert _source_deleted_path("no-such-branch", "feature.py") is False


class TestPrePassBookkeeping:
    """Cases inherited from the ordered-mock suite this file replaces.

    Those mocks scripted an exact subprocess sequence and passed while the
    real `git rm` was refusing every deletion ("changes staged in the index",
    swallowed into a warning). Same cases, exercised against real repos.
    """

    def test_empty_when_source_deleted_nothing(self, repo, monkeypatch):
        """No deletions on source → nothing propagated."""
        _premerge(repo)

        assert _run_propagate(repo, monkeypatch) == []

    def test_skips_a_path_already_absent_from_the_index(self, repo, monkeypatch):
        """A deletion resolved earlier in the merge is not re-attempted."""
        _delete_feature_on_dev(repo)
        _premerge(repo)
        subprocess.run(
            ["git", "rm", "-f", "--", "feature.py"],
            cwd=repo,
            capture_output=True,
            check=True,
        )

        assert _run_propagate(repo, monkeypatch) == []

    def test_multiple_deletions_are_all_propagated(self, repo, monkeypatch):
        """Two source-side deletions, one target-owned file, all handled."""
        _git("checkout", "-q", "dev", cwd=repo)
        (repo / "second.py").write_text("x = 1\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "add second", cwd=repo)

        _git("checkout", "-q", "staging", cwd=repo)
        _git("checkout", "dev", "--", ".", cwd=repo)
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "promote second", cwd=repo)

        _git("checkout", "-q", "dev", cwd=repo)
        (repo / "second.py").unlink()
        (repo / "feature.py").unlink()
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "drop both", cwd=repo)
        _git("push", "-q", "origin", "dev", "staging", cwd=repo)
        _git("fetch", "-q", "origin", cwd=repo)
        _premerge(repo)

        propagated = _run_propagate(repo, monkeypatch)

        assert sorted(propagated) == ["feature.py", "second.py"]
        assert "RELEASE_NOTES.md" in _git("ls-files", cwd=repo).stdout.split()

    def test_git_rm_uses_force(self, repo, monkeypatch):
        """The pre-merge stages the file's addition, so plain `git rm` refuses.

        Regression guard: without -f the deletion silently degrades to a
        warning and the file resurrects anyway.
        """
        _delete_feature_on_dev(repo)
        _premerge(repo)
        plain = subprocess.run(
            ["git", "rm", "--", "feature.py"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        assert plain.returncode != 0, "precondition: plain git rm should refuse"
        assert "staged in the index" in plain.stderr

        assert _run_propagate(repo, monkeypatch) == ["feature.py"]

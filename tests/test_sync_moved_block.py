"""A block source moved after promoting it must not be duplicated (#430).

Under squash promotion the merge-base never advances past the original fork
point. When source appends a block, promotes it, and then moves it, the
pre-merge sees two unrelated changes against that ancient base: source inserts
the block (and six lines) at the top, target appends it at the bottom. They do
not overlap, so git merges them cleanly, exit 0, and the block appears twice.
The merged blob equals *neither* side, so the v0.50.0 revert pass, which acted
only when the merge took target's copy whole, let it through silently.

Measured on the fixture below (git 2.54): the pre-merge exits 0, ``f.py`` has a
stage-0 entry, and ``def moved():`` sits at lines 7 and 29 of the merged file.

The promotions are tree copies (``git checkout dev -- .``), which is what a
squash-merged sync PR leaves on target. ``git merge --squash`` would conflict on
the second promotion for the very reason this bug exists, and the fixture would
prove nothing.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from fraisier.cli.sync import (
    _STALE_TARGET_MERGED,
    _propagate_source_reverts,
)

BLOCK = "def moved():\n    return 1\n"
PREAMBLE = "".join(f"pre {n}\n" for n in range(1, 7))


def _lines(prefix: str) -> str:
    return "".join(f"{prefix} {n}\n" for n in range(1, 21))


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert text.count(old) == 1, f"{old!r} must be unique in {path.name}"
    path.write_text(text.replace(old, new))


def _commit(work: Path, message: str) -> None:
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", message, cwd=work)


def _promote(work: Path, label: str) -> None:
    """Target's tree becomes source's tree in one commit: a squash-merged PR."""
    _git("checkout", "-q", "staging", cwd=work)
    _git("checkout", "dev", "--", ".", cwd=work)
    _commit(work, f"Promote dev -> staging ({label})")
    _git("checkout", "-q", "dev", cwd=work)


def _move_block_to_top(path: Path) -> None:
    text = path.read_text()
    assert text.endswith(BLOCK)
    path.write_text(PREAMBLE + BLOCK + text.removesuffix(BLOCK))


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """`dev` and `staging` related only by squash promotion. Three subjects:

    * ``f.py`` — dev appended a block, promoted it, then moved it to the top
      behind six new lines. The bug as filed.
    * ``g.py`` — the same move, but staging also hotfixed line 10. That hotfix
      is staging's own content, so the clean three-way merge must stand.
    * ``r.py`` — the revert-hotfix boundary. v1 adds X; v2 drops X and edits
      line 3; both promoted; staging restores v1 as a hotfix; dev edits line 10.
      Staging's blob *is* in dev's history, so it reads as source-derived.

    All three merge cleanly, so all three reach the pass. The fixture chdirs into
    the work tree because the code under test shells out to git in the cwd.
    """
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "-b", "dev", cwd=work)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "T", cwd=work)
    _git("config", "commit.gpgsign", "false", cwd=work)

    for name in ("f", "g", "r"):
        (work / f"{name}.py").write_text(_lines(name))
    _commit(work, "base")
    _git("branch", "staging", cwd=work)

    for name in ("f", "g"):
        (work / f"{name}.py").write_text(_lines(name) + BLOCK)
    (work / "r.py").write_text(_lines("r") + "X added in v1\n")
    _commit(work, "v1: add the block, add X")
    _promote(work, "v1")

    _edit(work / "r.py", "X added in v1\n", "")
    _edit(work / "r.py", "r 3\n", "r 3 v2\n")
    _commit(work, "v2: drop X, edit line 3")
    _promote(work, "v2")

    _git("checkout", "-q", "staging", cwd=work)
    (work / "r.py").write_text(_lines("r") + "X added in v1\n")
    _edit(work / "g.py", "g 10\n", "g 10 staging hotfix\n")
    _commit(work, "staging hotfixes: restore r.py v1, edit g.py")

    _git("checkout", "-q", "dev", cwd=work)
    _move_block_to_top(work / "f.py")
    _move_block_to_top(work / "g.py")
    _edit(work / "r.py", "r 10\n", "r 10 v3\n")
    _commit(work, "v3: move the block up, edit r.py line 10")

    bare = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(bare), cwd=work)
    _git("remote", "add", "origin", str(bare), cwd=work)
    _git("push", "-q", "origin", "dev", "staging", cwd=work)
    _git("fetch", "-q", "origin", cwd=work)
    monkeypatch.chdir(work)
    return work


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


def _stage0(work: Path, path: str) -> str:
    """The merged index blob. Fails the test if *path* is still unmerged."""
    entry = _git("ls-files", "-s", "--", path, cwd=work).stdout.split()
    assert entry, f"{path} is not in the index"
    assert entry[2] == "0", f"{path} conflicted; the pass would never see it"
    return entry[1]


def _rev(work: Path, ref: str) -> str:
    return _git("rev-parse", ref, cwd=work).stdout.strip()


class TestTheDuplication:
    """The bug as filed, before any fix gets a say."""

    def test_premerge_is_clean(self, repo):
        result = _premerge(repo)

        assert result.returncode == 0, result.stdout + result.stderr

    def test_merged_blob_equals_neither_side(self, repo):
        _premerge(repo)

        merged = _stage0(repo, "f.py")
        assert merged != _rev(repo, "origin/staging:f.py")
        assert merged != _rev(repo, "origin/dev:f.py")

    def test_the_merge_alone_duplicates_the_block(self, repo):
        _premerge(repo)
        _stage0(repo, "f.py")

        assert (repo / "f.py").read_text().count("def moved():") == 2


class TestTheMovedBlockTakesSource:
    def test_block_appears_once(self, repo):
        _premerge(repo)
        _stage0(repo, "f.py")
        _propagate_source_reverts("dev", "staging")

        assert (repo / "f.py").read_text().count("def moved():") == 1

    def test_index_holds_source_blob(self, repo):
        _premerge(repo)
        _stage0(repo, "f.py")
        _propagate_source_reverts("dev", "staging")

        assert _stage0(repo, "f.py") == _rev(repo, "origin/dev:f.py")

    def test_recorded_under_its_own_label(self, repo):
        """Not "source revert": source reverted nothing here."""
        _premerge(repo)
        _stage0(repo, "f.py")

        assert _propagate_source_reverts("dev", "staging")["f.py"] is (
            _STALE_TARGET_MERGED
        )


class TestTargetHotfixInAMovedFile:
    """Staging authored g.py's line 10, so the three-way result stands."""

    def test_merged_file_is_kept(self, repo):
        _premerge(repo)
        merged = _stage0(repo, "g.py")
        restored = _propagate_source_reverts("dev", "staging")

        assert "g.py" not in restored
        assert _stage0(repo, "g.py") == merged
        assert "g 10 staging hotfix" in (repo / "g.py").read_text()

    def test_warning_names_it(self, repo, capsys):
        _premerge(repo)
        _stage0(repo, "g.py")
        _propagate_source_reverts("dev", "staging")

        err = capsys.readouterr().err
        assert "g.py" in err
        assert "not source-derived" in err
        assert "keeping the three-way merge" in err


class TestRevertHotfixBoundary:
    """Staging restored an earlier dev version of r.py as a hotfix.

    Before #430 the clean merge produced a silent **blend**: X (the hotfix's
    addition against the ancient base) survived, while line 3 stayed at v2 (the
    hotfix's restoration of base content was dropped). That was neither the
    hotfix nor source. The owner's call (D4): take source's file and name it.
    """

    def test_premerge_blends_today(self, repo):
        """The "before", pinned so the shape cannot drift into a conflict."""
        _premerge(repo)
        _stage0(repo, "r.py")
        text = (repo / "r.py").read_text()

        assert "X added in v1" in text
        assert "r 3 v2\n" in text
        assert "r 10 v3\n" in text

    def test_source_file_is_taken(self, repo):
        _premerge(repo)
        _stage0(repo, "r.py")
        restored = _propagate_source_reverts("dev", "staging")

        assert restored["r.py"] is _STALE_TARGET_MERGED
        assert _stage0(repo, "r.py") == _rev(repo, "origin/dev:r.py")

    def test_warning_names_the_overwritten_hotfix(self, repo, capsys):
        _premerge(repo)
        _stage0(repo, "r.py")
        _propagate_source_reverts("dev", "staging")

        err = capsys.readouterr().err
        assert "r.py" in err
        assert "restored an earlier dev version" in err

    def test_a_plain_stale_copy_is_not_called_a_hotfix(self, repo, capsys):
        _premerge(repo)
        _propagate_source_reverts("dev", "staging")

        err = capsys.readouterr().err
        assert "f.py" not in err

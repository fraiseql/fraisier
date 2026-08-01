"""`get_worktree_sha` must work in fraisier's own deploy model (#321).

The bare-repo + worktree layout leaves the worktree with **no `.git`
directory** — which is why `fetch_and_checkout` passes `--git-dir/--work-tree`.
`get_worktree_sha` did not, so `git -C <worktree> rev-parse HEAD` raised
`fatal: not a git repository`, was caught, and returned None on *every* deploy
rather than only the first.

`_previous_sha` is assigned from it and nothing else, so every rollback path —
git revert, service restart, database rollback target — was a permanent no-op.
A failed deploy left the worktree and venv ahead of the database.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from fraisier.git.operations import fetch_and_checkout, get_worktree_sha


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def bare_and_worktree(tmp_path):
    """A bare mirror + worktree, exactly the shape fraisier deploys into."""
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", ".", cwd=src)
    _git("config", "user.email", "t@e.st", cwd=src)
    _git("config", "user.name", "t", cwd=src)
    (src / "f.txt").write_text("v1\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-qm", "one", cwd=src)

    bare = tmp_path / "bare.git"
    _git("clone", "-q", "--bare", str(src), str(bare))
    # A plain --bare clone has no refs/remotes/origin/*, so `origin/<branch>`
    # would not resolve. Production bare repos do resolve it (fetch_and_checkout
    # rev-parses it), so give the fixture the same refspec.
    _git(
        f"--git-dir={bare}",
        "config",
        "remote.origin.fetch",
        "+refs/heads/*:refs/remotes/origin/*",
    )
    _git(f"--git-dir={bare}", "fetch", "-q", "origin")
    branch = _git("-C", str(src), "rev-parse", "--abbrev-ref", "HEAD")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git(f"--git-dir={bare}", f"--work-tree={worktree}", "checkout", "-f", "HEAD")
    return bare, worktree, src, branch


class TestBareRepoWorktree:
    def test_worktree_really_has_no_dot_git(self, bare_and_worktree):
        """The premise — if this changes, the whole bug class changes."""
        _bare, worktree, _src, _branch = bare_and_worktree

        assert not (worktree / ".git").exists()

    def test_sha_is_readable_with_the_bare_repo(self, bare_and_worktree):
        bare, worktree, _src, _branch = bare_and_worktree
        expected = _git(f"--git-dir={bare}", "rev-parse", "HEAD")

        assert get_worktree_sha(worktree, bare_repo=bare) == expected

    def test_previous_sha_survives_a_second_deploy(self, bare_and_worktree):
        """The actual regression: deploy twice, old_sha must be the first commit."""
        bare, worktree, src, branch = bare_and_worktree
        first = _git(f"--git-dir={bare}", "rev-parse", "HEAD")

        (src / "f.txt").write_text("v2\n")
        _git("add", "-A", cwd=src)
        _git("commit", "-qm", "two", cwd=src)

        old_sha, new_sha = fetch_and_checkout(bare, worktree, branch)

        assert old_sha == first, "old_sha lost — rollback would have no target"
        assert new_sha != first


class TestStillCorrectElsewhere:
    def test_plain_clone_worktree_still_works(self, tmp_path):
        """A worktree that does have .git must keep working without a bare repo."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", "-q", ".", cwd=repo)
        _git("config", "user.email", "t@e.st", cwd=repo)
        _git("config", "user.name", "t", cwd=repo)
        (repo / "f.txt").write_text("x\n")
        _git("add", "-A", cwd=repo)
        _git("commit", "-qm", "one", cwd=repo)

        assert get_worktree_sha(repo) == _git("-C", str(repo), "rev-parse", "HEAD")

    def test_genuinely_first_deploy_returns_none(self, tmp_path):
        """An empty bare repo has no HEAD — that is a real None, keep it."""
        bare = tmp_path / "empty.git"
        _git("init", "-q", "--bare", str(bare))
        worktree = tmp_path / "wt"
        worktree.mkdir()

        assert get_worktree_sha(worktree, bare_repo=bare) is None

    def test_nonexistent_paths_return_none_not_raise(self, tmp_path):
        assert get_worktree_sha(tmp_path / "nope", bare_repo=tmp_path / "nah") is None

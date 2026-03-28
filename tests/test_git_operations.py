"""Tests for bare repo + worktree git operations."""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from fraisier.git.operations import (
    clone_bare_repo,
    fetch_and_checkout,
    get_worktree_sha,
)

REPOS_BASE = Path("/var/lib/fraisier/repos")


class TestGetWorktreeSha:
    """Test reading the current SHA from a worktree."""

    def test_returns_sha_from_git_rev_parse(self):
        mock_result = MagicMock()
        mock_result.stdout = "abc1234def5678\n"
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            sha = get_worktree_sha(Path("/srv/myapp"))
        mock_run.assert_called_once_with(
            ["git", "-C", "/srv/myapp", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert sha == "abc1234def5678"

    def test_returns_none_when_no_git_repo(self):
        import subprocess

        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(128, "git"),
        ):
            sha = get_worktree_sha(Path("/srv/noapp"))
        assert sha is None


class TestCloneBareRepo:
    """Test initial bare repo clone on first deploy."""

    def test_clones_bare_repo_when_not_exists(self, tmp_path):
        bare_repo = tmp_path / "myapp.git"
        clone_url = "git@github.com:org/myapp.git"

        with patch("subprocess.run") as mock_run:
            clone_bare_repo(clone_url, bare_repo)

        mock_run.assert_called_once_with(
            ["git", "clone", "--bare", clone_url, str(bare_repo)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_skips_clone_when_repo_exists(self, tmp_path):
        bare_repo = tmp_path / "myapp.git"
        bare_repo.mkdir()

        with patch("subprocess.run") as mock_run:
            clone_bare_repo("git@github.com:org/myapp.git", bare_repo)

        mock_run.assert_not_called()


class TestFetchAndCheckout:
    """Test fetch + checkout updates worktree files."""

    def _mock_run(self, old_sha="aaa1111", new_sha="bbb2222"):
        """Build a side_effect that returns appropriate values per command."""

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            # git rev-parse HEAD in worktree → old sha
            if cmd == ["git", "-C", str(self.worktree), "rev-parse", "HEAD"]:
                result.stdout = f"{old_sha}\n"
            # git rev-parse origin/main → new sha
            if "rev-parse" in cmd and any(arg.startswith("origin/") for arg in cmd):
                result.stdout = f"{new_sha}\n"
            return result

        return side_effect

    def setup_method(self):
        self.bare_repo = Path("/var/lib/fraisier/repos/myapp.git")
        self.worktree = Path("/srv/myapp")
        self.branch = "main"

    def test_returns_old_and_new_sha(self):
        with patch("subprocess.run", side_effect=self._mock_run()):
            old, new = fetch_and_checkout(self.bare_repo, self.worktree, self.branch)

        assert old == "aaa1111"
        assert new == "bbb2222"

    def test_fetches_from_origin(self):
        with patch("subprocess.run", side_effect=self._mock_run()) as mock_run:
            fetch_and_checkout(self.bare_repo, self.worktree, self.branch)

        fetch_call = call(
            [
                "git",
                "-C",
                str(self.bare_repo),
                "fetch",
                "origin",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert fetch_call in mock_run.call_args_list

    def test_checks_out_new_sha_to_worktree(self):
        with patch("subprocess.run", side_effect=self._mock_run()) as mock_run:
            fetch_and_checkout(self.bare_repo, self.worktree, self.branch)

        checkout_call = call(
            [
                "git",
                f"--work-tree={self.worktree}",
                f"--git-dir={self.bare_repo}",
                "checkout",
                "-f",
                "bbb2222",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert checkout_call in mock_run.call_args_list

    def test_resets_worktree_head_to_new_sha(self):
        """Critical: without reset --soft, git in the worktree reports stale state."""
        with patch("subprocess.run", side_effect=self._mock_run()) as mock_run:
            fetch_and_checkout(self.bare_repo, self.worktree, self.branch)

        reset_call = call(
            [
                "git",
                f"--work-tree={self.worktree}",
                f"--git-dir={self.bare_repo}",
                "reset",
                "--soft",
                "bbb2222",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert reset_call in mock_run.call_args_list

    def test_returns_none_old_sha_on_fresh_worktree(self):
        """First deploy: worktree has no commits yet."""
        import subprocess

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            # First rev-parse (worktree HEAD) fails
            if cmd == [
                "git",
                "-C",
                str(self.worktree),
                "rev-parse",
                "HEAD",
            ]:
                raise subprocess.CalledProcessError(128, "git")
            # rev-parse origin/main succeeds
            if "rev-parse" in cmd and any(arg.startswith("origin/") for arg in cmd):
                result.stdout = "bbb2222\n"
            return result

        with patch("subprocess.run", side_effect=side_effect):
            old, new = fetch_and_checkout(self.bare_repo, self.worktree, self.branch)

        assert old is None
        assert new == "bbb2222"

    def test_reset_uses_git_dir_not_worktree(self):
        """reset --soft must use --git-dir, not git -C worktree."""
        with patch("subprocess.run", side_effect=self._mock_run()) as mock_run:
            fetch_and_checkout(self.bare_repo, self.worktree, self.branch)

        reset_call = call(
            [
                "git",
                f"--work-tree={self.worktree}",
                f"--git-dir={self.bare_repo}",
                "reset",
                "--soft",
                "bbb2222",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert reset_call in mock_run.call_args_list

    def test_previous_sha_available_for_rollback(self):
        """The old SHA returned can be used for rollback."""
        with patch("subprocess.run", side_effect=self._mock_run()):
            old, new = fetch_and_checkout(self.bare_repo, self.worktree, self.branch)

        assert old is not None
        assert old != new
        assert old == "aaa1111"

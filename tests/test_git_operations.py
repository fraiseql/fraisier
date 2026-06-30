"""Tests for bare repo + worktree git operations."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from fraisier.errors import DeploymentError
from fraisier.git.operations import (
    _should_escalate,
    clone_bare_repo,
    fetch_and_checkout,
    force_repopulate_worktree,
    get_worktree_sha,
    verify_worktree_at_sha,
)
from tests.fixtures.git_env import DeployEnv

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

    def test_verifies_worktree_matches_new_sha_after_checkout(self):
        """A checkout that exits 0 but leaves stale files must be caught: the
        deploy diffs the worktree against new_sha before recording a version."""
        with patch("subprocess.run", side_effect=self._mock_run()) as mock_run:
            fetch_and_checkout(self.bare_repo, self.worktree, self.branch)

        verify_call = call(
            [
                "git",
                f"--work-tree={self.worktree}",
                f"--git-dir={self.bare_repo}",
                "diff",
                "--quiet",
                "bbb2222",
                "--",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert verify_call in mock_run.call_args_list

    def test_raises_when_worktree_stale_after_checkout(self):
        """Frozen worktree (checkout silently no-ops): version must NOT advance."""

        def side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if cmd == ["git", "-C", str(self.worktree), "rev-parse", "HEAD"]:
                result.stdout = "aaa1111\n"
            if "rev-parse" in cmd and any(a.startswith("origin/") for a in cmd):
                result.stdout = "bbb2222\n"
            # Verification diff reports the worktree differs from new_sha.
            if "diff" in cmd and "--quiet" in cmd:
                result.returncode = 1
            if "diff" in cmd and "--name-only" in cmd:
                result.stdout = "app.py\nconfig/settings.toml\n"
            return result

        with (
            patch("subprocess.run", side_effect=side_effect),
            pytest.raises(DeploymentError) as exc_info,
        ):
            fetch_and_checkout(self.bare_repo, self.worktree, self.branch)

        err = exc_info.value
        assert err.context["expected_sha"] == "bbb2222"
        assert "app.py" in err.context["stale_files"]


class TestVerifyWorktreeAtSha:
    """Guard added after the frozen-staging-worktree incident: confirm the
    worktree's files actually match the deployed SHA, using a real git repo."""

    def test_passes_when_worktree_matches_sha(self, git_deploy_env: DeployEnv):
        """Fixture worktree is checked out at v1 — verifying v1 must not raise."""
        verify_worktree_at_sha(
            git_deploy_env.bare_repo,
            git_deploy_env.worktree,
            git_deploy_env.sha_v1,
        )

    def test_raises_when_worktree_is_stale(self, git_deploy_env: DeployEnv):
        """Worktree files are v1; verifying against v2 must fail loudly."""
        with pytest.raises(DeploymentError) as exc_info:
            verify_worktree_at_sha(
                git_deploy_env.bare_repo,
                git_deploy_env.worktree,
                git_deploy_env.sha_v2,
            )

        err = exc_info.value
        assert err.context["expected_sha"] == git_deploy_env.sha_v2
        assert "app.py" in err.context["stale_files"]
        assert str(git_deploy_env.worktree) == err.context["worktree"]

    def test_passes_after_real_checkout_to_v2(self, git_deploy_env: DeployEnv):
        """The healthy path: after an actual checkout to v2, verification passes."""
        subprocess.run(
            [
                "git",
                f"--work-tree={git_deploy_env.worktree}",
                f"--git-dir={git_deploy_env.bare_repo}",
                "checkout",
                "-f",
                git_deploy_env.sha_v2,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        verify_worktree_at_sha(
            git_deploy_env.bare_repo,
            git_deploy_env.worktree,
            git_deploy_env.sha_v2,
        )

    def test_force_repopulate_recovers_stale_worktree(self, git_deploy_env: DeployEnv):
        """The self-heal mechanism: read-tree --reset -u rewrites the tracked
        files to the target commit, recovering a worktree the porcelain checkout
        left stale. Worktree is at v1; force-repopulate to v2 → verify passes."""
        force_repopulate_worktree(
            git_deploy_env.bare_repo,
            git_deploy_env.worktree,
            git_deploy_env.sha_v2,
        )

        verify_worktree_at_sha(
            git_deploy_env.bare_repo,
            git_deploy_env.worktree,
            git_deploy_env.sha_v2,
        )


class TestShouldEscalate:
    """The bounded-recovery decision: re-mismatch within N deploys → fail hard."""

    def test_never_healed_does_not_escalate(self):
        assert _should_escalate(deploy_no=1, last_heal_deploy=None, within=3) is False

    def test_recurrence_within_window_escalates(self):
        # healed at deploy 1, mismatch again at deploy 2 → within 3 → escalate
        assert _should_escalate(deploy_no=2, last_heal_deploy=1, within=3) is True

    def test_window_boundary_escalates(self):
        # exactly N deploys later still counts as "within"
        assert _should_escalate(deploy_no=4, last_heal_deploy=1, within=3) is True

    def test_aged_out_heals_again(self):
        # more than N deploys since the heal → treat as a fresh incident
        assert _should_escalate(deploy_no=5, last_heal_deploy=1, within=3) is False


class TestSelfHealEscalation:
    """fetch_and_checkout heals a stale worktree once, then escalates if the
    same worktree mismatches again within the escalation window."""

    branch = "main"

    def _env(self, tmp_path):
        bare = tmp_path / "bare.git"
        bare.mkdir()
        return bare, tmp_path / "wt"

    def _side_effect(self, worktree, diff_quiet_codes, *, new="bbb2222", old="aaa1111"):
        """subprocess mock: drives `git diff --quiet` returncodes from a shared
        queue so a sequence of deploys can be simulated across calls."""
        codes = iter(diff_quiet_codes)

        def se(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            if cmd == ["git", "-C", str(worktree), "rev-parse", "HEAD"]:
                r.stdout = f"{old}\n"
            if "rev-parse" in cmd and any(a.startswith("origin/") for a in cmd):
                r.stdout = f"{new}\n"
            if "diff" in cmd and "--quiet" in cmd:
                r.returncode = next(codes)
            if "diff" in cmd and "--name-only" in cmd:
                r.stdout = "app.py\n"
            return r

        return se

    def _state(self, bare, worktree):
        data = json.loads((bare / "fraisier_selfheal_state.json").read_text())
        return data[str(worktree)]

    def test_heals_once_on_first_mismatch(self, tmp_path):
        bare, wt = self._env(tmp_path)
        # deploy 1: first verify mismatches, post-repopulate verify passes.
        se = self._side_effect(wt, [1, 0])
        with patch("subprocess.run", side_effect=se) as mock_run:
            old, new = fetch_and_checkout(bare, wt, self.branch)

        assert (old, new) == ("aaa1111", "bbb2222")
        # the forced repopulate ran (read-tree --reset -u)
        assert any("read-tree" in c.args[0] for c in mock_run.call_args_list if c.args)
        # the heal was recorded against this deploy
        assert self._state(bare, wt)["last_heal_deploy"] == 1

    def test_escalates_on_recurring_mismatch_within_window(self, tmp_path):
        bare, wt = self._env(tmp_path)
        # d1: [1,0] heal; d2: [1] mismatch again → escalate (no repopulate).
        se = self._side_effect(wt, [1, 0, 1])
        with patch("subprocess.run", side_effect=se):
            fetch_and_checkout(bare, wt, self.branch)  # heals
            with pytest.raises(DeploymentError) as exc_info:
                fetch_and_checkout(bare, wt, self.branch)  # escalates

        err = exc_info.value
        assert err.context["deploy_number"] == 2
        assert err.context["last_heal_deploy"] == 1
        assert "recur" in str(err).lower() or "again" in str(err).lower()

    def test_heals_again_after_escalation_window_passes(self, tmp_path):
        bare, wt = self._env(tmp_path)
        # d1 heal [1,0]; d2-d4 clean [0,0,0]; d5 mismatch [1,0] → heal again
        # (5 - 1 = 4 > 3, so the prior heal has aged out).
        se = self._side_effect(wt, [1, 0, 0, 0, 0, 1, 0])
        with patch("subprocess.run", side_effect=se):
            for _ in range(5):
                fetch_and_checkout(bare, wt, self.branch)

        entry = self._state(bare, wt)
        assert entry["deploys"] == 5
        assert entry["last_heal_deploy"] == 5

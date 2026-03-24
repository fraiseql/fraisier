"""Tests for the git_deploy_env fixture itself.

Validates that the fixture creates a correct git environment:
bare repo, worktree, two distinct commits, compatible with fraisier's
git operations module.
"""

import subprocess

from fraisier.git.operations import get_worktree_sha
from tests.fixtures.git_env import DeployEnv


class TestGitDeployEnv:
    """Validate the git deploy environment fixture."""

    def test_bare_repo_exists(self, git_deploy_env: DeployEnv):
        """Bare repo directory exists and is a bare git repo."""
        assert git_deploy_env.bare_repo.exists()
        assert (git_deploy_env.bare_repo / "HEAD").exists()

    def test_worktree_exists(self, git_deploy_env: DeployEnv):
        """Worktree directory exists with app files."""
        assert git_deploy_env.worktree.exists()
        assert (git_deploy_env.worktree / "app.py").exists()

    def test_worktree_at_v1(self, git_deploy_env: DeployEnv):
        """Worktree is checked out at v1."""
        content = (git_deploy_env.worktree / "app.py").read_text()
        assert 'VERSION = "v1"' in content

    def test_two_distinct_shas(self, git_deploy_env: DeployEnv):
        """v1 and v2 are different commits."""
        assert git_deploy_env.sha_v1 != git_deploy_env.sha_v2
        assert len(git_deploy_env.sha_v1) == 40
        assert len(git_deploy_env.sha_v2) == 40

    def test_bare_repo_has_both_commits(self, git_deploy_env: DeployEnv):
        """Both SHAs exist in the bare repo."""
        for sha in [git_deploy_env.sha_v1, git_deploy_env.sha_v2]:
            result = subprocess.run(
                ["git", "-C", str(git_deploy_env.bare_repo), "cat-file", "-t", sha],
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.stdout.strip() == "commit"

    def test_can_checkout_v2_into_worktree(self, git_deploy_env: DeployEnv):
        """Can checkout v2 into the worktree (simulating a deploy)."""
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
        content = (git_deploy_env.worktree / "app.py").read_text()
        assert 'VERSION = "v2"' in content

    def test_get_worktree_sha_returns_v1(self, git_deploy_env: DeployEnv):
        """Fraisier's get_worktree_sha reads the correct SHA from our fixture."""
        sha = get_worktree_sha(git_deploy_env.worktree)
        assert sha == git_deploy_env.sha_v1

    def test_status_dir_exists(self, git_deploy_env: DeployEnv):
        """Status directory exists for deployment status files."""
        assert git_deploy_env.status_dir.exists()
        assert git_deploy_env.status_dir.is_dir()

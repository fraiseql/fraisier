"""Real git bare repo + worktree fixtures for integration tests.

Creates a temporary git environment that mirrors a real fraisier deployment:
- A bare repo (simulating the remote)
- A worktree checkout (simulating the app_path)
- Multiple commits to simulate version progression
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class DeployEnv:
    """A real git deployment environment for testing."""

    bare_repo: Path
    worktree: Path
    sha_v1: str
    sha_v2: str
    status_dir: Path


def _git(cwd: Path, *args: str) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _create_bare_repo_with_commits(
    tmp_path: Path, num_commits: int = 2
) -> tuple[Path, list[str]]:
    """Create a bare repo with N commits, return (bare_path, [sha1, sha2, ...])."""
    # Create a temporary working repo to generate commits
    work_repo = tmp_path / "work_repo"
    work_repo.mkdir()
    _git(work_repo, "init", "--initial-branch", "main")
    _git(work_repo, "config", "user.email", "test@fraisier.dev")
    _git(work_repo, "config", "user.name", "Fraisier Test")

    shas = []
    for i in range(1, num_commits + 1):
        # Create a file to simulate a version
        app_file = work_repo / "app.py"
        app_file.write_text(f'VERSION = "v{i}"\n')
        _git(work_repo, "add", ".")
        _git(work_repo, "commit", "-m", f"Version {i}")
        sha = _git(work_repo, "rev-parse", "HEAD")
        shas.append(sha)

    # Clone as bare repo
    bare_repo = tmp_path / "app.git"
    _git(tmp_path, "clone", "--bare", str(work_repo), str(bare_repo))

    return bare_repo, shas


def _checkout_worktree(bare_repo: Path, worktree: Path, sha: str) -> None:
    """Checkout a specific SHA into a worktree directory.

    Creates a .git file pointing at the bare repo so that both
    ``git --work-tree/--git-dir`` and ``git -C worktree`` commands
    work correctly (matching fraisier's real deploy pattern).
    """
    worktree.mkdir(parents=True, exist_ok=True)

    # Point worktree's .git at the bare repo (like git worktree add)
    git_file = worktree / ".git"
    git_file.write_text(f"gitdir: {bare_repo}\n")

    # Checkout files from bare repo into worktree
    subprocess.run(
        [
            "git",
            f"--work-tree={worktree}",
            f"--git-dir={bare_repo}",
            "checkout",
            "-f",
            sha,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    # Update HEAD so git -C worktree reports correct SHA
    _git(worktree, "reset", "--soft", sha)


@pytest.fixture
def git_deploy_env(tmp_path: Path) -> DeployEnv:
    """Create a real git deployment environment with two commits.

    Returns a DeployEnv with:
    - bare_repo: a bare git repo with 2 commits
    - worktree: checked out at v1 (sha_v1)
    - sha_v1: first commit SHA
    - sha_v2: second commit SHA (newer, simulates incoming deploy)
    - status_dir: temp dir for deployment status files
    """
    bare_repo, shas = _create_bare_repo_with_commits(tmp_path, num_commits=2)
    worktree = tmp_path / "app"
    _checkout_worktree(bare_repo, worktree, shas[0])

    status_dir = tmp_path / "status"
    status_dir.mkdir()

    return DeployEnv(
        bare_repo=bare_repo,
        worktree=worktree,
        sha_v1=shas[0],
        sha_v2=shas[1],
        status_dir=status_dir,
    )

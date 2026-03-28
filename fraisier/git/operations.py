"""Bare repo + worktree git operations.

Uses the pattern: bare clone → fetch → checkout -f → reset --soft
to update a worktree without keeping a full .git directory in the
deployment path.
"""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("fraisier")


def get_worktree_sha(worktree: Path) -> str | None:
    """Read the current HEAD SHA from a worktree.

    Returns None if the worktree has no git state (first deploy).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def clone_bare_repo(clone_url: str, bare_repo: Path) -> None:
    """Clone a bare repo if it doesn't already exist."""
    if bare_repo.exists():
        return

    subprocess.run(
        ["git", "clone", "--bare", clone_url, str(bare_repo)],
        check=True,
        capture_output=True,
        text=True,
    )


def fetch_and_checkout(
    bare_repo: Path, worktree: Path, branch: str
) -> tuple[str | None, str]:
    """Fetch from origin and checkout into worktree.

    Returns (old_sha, new_sha). old_sha is None on first deploy.
    The old_sha can be used for rollback.
    """
    old_sha = get_worktree_sha(worktree)

    # Fetch latest from origin
    subprocess.run(
        ["git", "-C", str(bare_repo), "fetch", "origin"],
        check=True,
        capture_output=True,
        text=True,
    )

    # Resolve the new SHA
    result = subprocess.run(
        ["git", "-C", str(bare_repo), "rev-parse", f"origin/{branch}"],
        capture_output=True,
        text=True,
        check=True,
    )
    new_sha = result.stdout.strip()

    # Checkout into worktree
    subprocess.run(
        [
            "git",
            f"--work-tree={worktree}",
            f"--git-dir={bare_repo}",
            "checkout",
            "-f",
            new_sha,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    # Critical: update worktree HEAD so git reports correct state.
    # Use --git-dir/--work-tree to support bare repo + worktree pattern
    # where the worktree has no .git directory.
    subprocess.run(
        [
            "git",
            f"--work-tree={worktree}",
            f"--git-dir={bare_repo}",
            "reset",
            "--soft",
            new_sha,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    return old_sha, new_sha

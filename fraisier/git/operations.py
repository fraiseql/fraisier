"""Bare repo + worktree git operations.

Uses the pattern: bare clone → fetch → checkout -f → reset --soft
to update a worktree without keeping a full .git directory in the
deployment path.
"""

import logging
import subprocess
from pathlib import Path

from fraisier.errors import DeploymentError

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


def get_commit_timestamp(git_dir: Path, sha: str) -> str | None:
    """Get the author date for a commit as ISO string (YYYY-MM-DD HH:MM:SS ±HHMM).

    Args:
        git_dir: Path to git directory (bare repo or .git)
        sha: Commit SHA to look up

    Returns:
        ISO timestamp string or None if commit not found or git fails
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(git_dir), "log", "-1", "--format=%ci", sha],
            capture_output=True,
            text=True,
            check=True,
        )
        ts = result.stdout.strip()
        return ts or None
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

    # Confirm the worktree files actually reflect new_sha before the caller
    # records it as the deployed version. A checkout that exits 0 without
    # populating the worktree would otherwise advance the recorded version
    # over stale code (the frozen-worktree failure mode).
    verify_worktree_at_sha(bare_repo, worktree, new_sha)

    return old_sha, new_sha


def verify_worktree_at_sha(bare_repo: Path, worktree: Path, sha: str) -> None:
    """Verify the worktree's tracked files match ``sha`` exactly.

    After a checkout the working tree must reflect the target commit. If a
    checkout exits 0 without updating the worktree, the deploy would record a
    new version over stale code — the frozen-staging-worktree incident, where
    the worktree stayed pinned to an old commit while ``version.json`` advanced.

    The check diffs the working tree against ``sha`` using ``--git-dir`` /
    ``--work-tree`` so it works whether or not the worktree carries a ``.git``
    file, and only inspects tracked files (untracked build artifacts and
    virtualenvs are ignored).

    Args:
        bare_repo: Path to the bare repository.
        worktree: Path to the deployed worktree.
        sha: The commit the worktree is expected to match.

    Raises:
        DeploymentError: If the worktree does not match ``sha``.
    """
    result = subprocess.run(
        [
            "git",
            f"--work-tree={worktree}",
            f"--git-dir={bare_repo}",
            "diff",
            "--quiet",
            sha,
            "--",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return

    # Mismatch (or a diff error): collect the differing files for diagnostics.
    name_result = subprocess.run(
        [
            "git",
            f"--work-tree={worktree}",
            f"--git-dir={bare_repo}",
            "diff",
            "--name-only",
            sha,
            "--",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    stale_files = [line for line in name_result.stdout.splitlines() if line]

    raise DeploymentError(
        f"Worktree at {worktree} does not match deployed commit {sha[:8]} "
        f"after checkout ({len(stale_files)} file(s) differ). The working "
        "tree is frozen or only partially updated; refusing to record a new "
        "version over stale code.",
        context={
            "worktree": str(worktree),
            "bare_repo": str(bare_repo),
            "expected_sha": sha,
            "stale_files": stale_files[:20],
            "diff_returncode": result.returncode,
            "stderr": result.stderr.strip(),
        },
        recovery_hint=(
            "Inspect the worktree on the target host with "
            f"`git --git-dir={bare_repo} --work-tree={worktree} status`, then "
            "re-run the deploy. Recreate the worktree if its git state is "
            "corrupt."
        ),
    )

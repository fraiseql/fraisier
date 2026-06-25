"""Bare repo + worktree git operations.

Uses the pattern: bare clone → fetch → checkout -f → reset --soft
to update a worktree without keeping a full .git directory in the
deployment path.
"""

import json
import logging
import subprocess
from pathlib import Path

from fraisier.errors import DeploymentError

logger = logging.getLogger("fraisier")

# Self-heal policy: a worktree that the porcelain checkout leaves stale is
# force-repopulated once. But a self-heal can mask a recurring environmental
# cause (a read-only filesystem, wrong permissions, a flapping mount) behind a
# green deploy — the exact silent-recovery masking that the version-stamp
# rollback work (#257) set out to remove. So the recovery is bounded: if the
# *same* worktree mismatches again within this many deploys of its last heal,
# the deploy fails hard and alerts instead of healing again.
_SELFHEAL_ESCALATE_WITHIN_DEPLOYS = 3
_SELFHEAL_STATE_FILE = "fraisier_selfheal_state.json"


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
    _verify_or_self_heal(bare_repo, worktree, new_sha)

    return old_sha, new_sha


def _verify_or_self_heal(bare_repo: Path, worktree: Path, new_sha: str) -> None:
    """Verify the worktree matches ``new_sha``; self-heal once, then escalate.

    On a mismatch the worktree is force-repopulated from the tree and
    re-verified — recovering a worktree the porcelain checkout left stale. The
    recovery is bounded (heal-once-then-escalate): if the *same* worktree
    mismatched again within ``_SELFHEAL_ESCALATE_WITHIN_DEPLOYS`` deploys of its
    last heal, the deploy fails hard and alerts instead of healing again, so a
    recurring environmental cause is not masked behind a green deploy (#257).

    Raises:
        DeploymentError: if the worktree still mismatches after a heal, or if a
            recurring mismatch trips the escalation window.
    """
    state = _read_selfheal_state(bare_repo)
    key = str(worktree)
    entry = state.setdefault(key, {"deploys": 0, "last_heal_deploy": None})
    entry["deploys"] += 1
    deploy_no = entry["deploys"]

    try:
        verify_worktree_at_sha(bare_repo, worktree, new_sha)
        _write_selfheal_state(bare_repo, state)
        return
    except DeploymentError as mismatch:
        last_heal = entry.get("last_heal_deploy")
        if _should_escalate(deploy_no, last_heal, _SELFHEAL_ESCALATE_WITHIN_DEPLOYS):
            since = deploy_no - last_heal  # type: ignore[operator]
            logger.critical(
                "Worktree %s mismatched again at deploy %d, only %d deploy(s) "
                "after a self-heal (deploy %d) — refusing to mask a recurring "
                "cause; failing the deploy.",
                worktree,
                deploy_no,
                since,
                last_heal,
            )
            _write_selfheal_state(bare_repo, state)
            raise DeploymentError(
                f"Worktree at {worktree} mismatched commit {new_sha[:8]} again "
                f"only {since} deploy(s) after a self-heal. A forced repopulate "
                "recovered it once but the mismatch recurred — this points to a "
                "persistent environmental cause (read-only filesystem, bad "
                "permissions, a flapping mount), not a one-off bad checkout. "
                "Failing hard rather than silently self-healing every deploy.",
                context={
                    "worktree": str(worktree),
                    "bare_repo": str(bare_repo),
                    "expected_sha": new_sha,
                    "deploy_number": deploy_no,
                    "last_heal_deploy": last_heal,
                    "escalate_within_deploys": _SELFHEAL_ESCALATE_WITHIN_DEPLOYS,
                },
                recovery_hint=(
                    "Investigate the worktree host: check filesystem mount "
                    "options (read-only?), ownership/permissions on "
                    f"{worktree}, and disk health. Recreate the worktree once "
                    "the underlying cause is fixed."
                ),
            ) from mismatch

        # Heal once: force a clean repopulate straight from the tree, then
        # re-verify. A still-stale worktree re-raises (the loud abort stands).
        logger.warning(
            "Worktree %s did not match %s after checkout; forcing repopulate "
            "(self-heal, deploy %d)",
            worktree,
            new_sha[:8],
            deploy_no,
        )
        force_repopulate_worktree(bare_repo, worktree, new_sha)
        verify_worktree_at_sha(bare_repo, worktree, new_sha)
        entry["last_heal_deploy"] = deploy_no
        _write_selfheal_state(bare_repo, state)
        logger.warning(
            "Worktree %s recovered to %s via forced repopulate at deploy %d",
            worktree,
            new_sha[:8],
            deploy_no,
        )


def _should_escalate(deploy_no: int, last_heal_deploy: int | None, within: int) -> bool:
    """Whether a mismatch should fail hard instead of self-healing again.

    ``True`` when this worktree was self-healed within the last ``within``
    deploys — a recurrence the bounded recovery must not keep papering over.
    """
    return last_heal_deploy is not None and (deploy_no - last_heal_deploy) <= within


def _selfheal_state_path(bare_repo: Path) -> Path:
    return bare_repo / _SELFHEAL_STATE_FILE


def _read_selfheal_state(bare_repo: Path) -> dict:
    """Read the per-worktree self-heal state, or ``{}`` if absent/unreadable."""
    try:
        data = json.loads(_selfheal_state_path(bare_repo).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_selfheal_state(bare_repo: Path, state: dict) -> None:
    """Persist the self-heal state. Best-effort — a write failure only weakens
    the escalation bound, it must not fail an otherwise-healthy deploy."""
    try:
        _selfheal_state_path(bare_repo).write_text(json.dumps(state))
    except OSError as exc:
        logger.warning("Could not persist self-heal state in %s: %s", bare_repo, exc)


def force_repopulate_worktree(bare_repo: Path, worktree: Path, sha: str) -> None:
    """Forcibly rewrite the worktree's tracked files to match ``sha``.

    Stronger and more deterministic than ``git checkout``: ``read-tree --reset
    -u`` discards whatever index and working-tree state is present and writes
    the tree directly, then HEAD is moved explicitly with ``update-ref``. This
    recovers a worktree the normal checkout path left stale or partially
    updated.

    Like the rest of this module it only touches tracked files: untracked
    generated files (``version.json``), virtualenvs, and build output are left
    in place. It does not delete a worktree or remove untracked data.

    Args:
        bare_repo: Path to the bare repository.
        worktree: Path to the deployed worktree.
        sha: The commit to materialize into the worktree.
    """
    subprocess.run(
        [
            "git",
            f"--work-tree={worktree}",
            f"--git-dir={bare_repo}",
            "read-tree",
            "--reset",
            "-u",
            sha,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", f"--git-dir={bare_repo}", "update-ref", "--no-deref", "HEAD", sha],
        check=True,
        capture_output=True,
        text=True,
    )


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

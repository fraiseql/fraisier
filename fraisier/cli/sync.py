"""sync command — promote source branch → target branch via a sync PR."""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import TYPE_CHECKING, NamedTuple

import click

from ._helpers import console, err_console, require_config
from .main import main

if TYPE_CHECKING:
    from fraisier.config.schema import SyncPair

# Files/prefixes that fraisier owns; always resolved from the source branch.
_AUTO_RESOLVED = (
    "version.json",
    "pyproject.toml",
    "uv.lock",
    ".secrets.baseline",
    "fraises.yaml",
    "scripts/generated",
)


class _Resolution(NamedTuple):
    """One auto-resolution tier: how it prints a file, how it counts a run.

    Every ``message`` names the *outcome* — which branch's content is in the
    tree now — and only then its reason. A tier that stated its justification
    in that slot (#364) read, on a 27-file promote, as "your outgoing changes
    were discarded" and cost a full audit mid-release. A new tier belongs in
    this block, in this shape.
    """

    message: str
    singular: str
    plural: str


_SOURCE_DELETION = _Resolution(
    "  Auto-resolved (source deletion): {path}",
    "source deletion",
    "source deletions",
)
_SOURCE_REVERT = _Resolution(
    "  Auto-resolved (source revert): {path}",
    "source revert",
    "source reverts",
)
_FRAISIER_OWNED = _Resolution(
    "  Auto-resolved (took {source}; fraisier-owned): {path}",
    "fraisier-owned file",
    "fraisier-owned files",
)
_STALE_TARGET = _Resolution(
    "  Auto-resolved (took {source}; {target} held a stale copy of it): {path}",
    "stale target copy",
    "stale target copies",
)
_PREFER_SOURCE = _Resolution(
    "  Auto-resolved (prefer-source): {path}",
    "by --prefer-source",
    "by --prefer-source",
)

#: Summary order. Fixed rather than by count, so two runs of the same promote
#: read the same way.
_RESOLUTION_ORDER = (
    _STALE_TARGET,
    _FRAISIER_OWNED,
    _SOURCE_DELETION,
    _SOURCE_REVERT,
    _PREFER_SOURCE,
)


class _AutoResolutions:
    """Per-file lines under --verbose, one counted line by default.

    Tier 1 is recorded here even though it printed nothing before this
    existed: a count that silently omits a tier is the same class of
    misleading output as a line that names the wrong side.
    """

    def __init__(self, *, verbose: bool, source: str, target: str) -> None:
        self._verbose = verbose
        self._source = source
        self._target = target
        self._counts: dict[_Resolution, int] = {}

    def record(self, kind: _Resolution, path: str) -> None:
        self._counts[kind] = self._counts.get(kind, 0) + 1
        if self._verbose:
            console.print(
                kind.message.format(source=self._source, target=self._target, path=path)
            )

    def summarise(self) -> None:
        total = sum(self._counts.values())
        if not total or self._verbose:
            return
        parts = [
            f"{n} {kind.singular if n == 1 else kind.plural}"
            for kind in _RESOLUTION_ORDER
            if (n := self._counts.get(kind, 0))
        ]
        console.print(
            f"  Auto-resolved {total} file(s): {', '.join(parts)}"
            "  [dim](--verbose lists them)[/dim]"
        )


def _is_auto_resolved(path: str) -> bool:
    """Return True if *path* is a fraisier-owned file that can be auto-resolved."""
    for pattern in _AUTO_RESOLVED:
        if path == pattern or path.startswith(pattern + "/"):
            return True
    return False


#: Cap on how far back to walk source's history for one path when deciding
#: whether target's copy came from source. Exhausting it is treated as "no
#: match" — conservative in the safe direction (we leave the file alone).
_SOURCE_HISTORY_SCAN_LIMIT = 200


def _z_lines(result: subprocess.CompletedProcess) -> list[str]:
    """Split NUL-delimited git output, dropping empties.

    ``-z`` avoids both the quoting ``core.quotepath`` applies to non-ASCII
    paths and the ambiguity of splitting on newlines when a path contains one.
    """
    return [item for item in (result.stdout or "").split("\0") if item]


def _diff_paths(tgt: str, source: str, diff_filter: str) -> list[str]:
    """Paths differing between ``origin/<tgt>`` and ``origin/<source>``.

    Shared by both #290 pre-passes — ``D`` for deletions, ``M`` for content
    reverts. Computed target-side rather than from ``merge-base..source``:
    sync PRs are squash-merged, so the merge-base never advances past the
    original fork point and anchoring on it is what hid the deletions.

    ``--no-renames`` is required, not cosmetic: ``git diff`` applies rename
    detection by default, which reports a rename as ``R`` rather than ``D``+``A``
    and would hide every rename-shaped deletion from the scan.
    """
    return _z_lines(
        subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                f"--diff-filter={diff_filter}",
                f"origin/{tgt}",
                f"origin/{source}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    )


def _target_blob_is_source_derived(source: str, tgt: str, path: str) -> bool:
    """Return True when target's copy of *path* is content it received from source.

    This replaces the old merge-base comparison, which is unusable here: sync
    PRs are squash-merged, so ``git merge-base origin/<source> origin/<tgt>``
    never advances past the original fork point no matter how many promotions
    run. Anchoring on it made this answer False for almost every path.

    The question that actually matters is not "has target changed this since
    some ancestor" but "is target holding a stale copy of *source's* content, or
    did target author its own version?". So: take target's blob for the path and
    look for it anywhere in source's history of that path.

    A match means target's copy originated on source — safe to take source's
    side. No match means target has its own content — leave it for the operator.

    Any git error returns False, so a bad signal never authorises an edit or a
    deletion. Same posture as the helper this replaces: fail closed.
    """
    target_blob = subprocess.run(
        ["git", "rev-parse", f"origin/{tgt}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if target_blob.returncode != 0:
        return False
    wanted = target_blob.stdout.strip()

    commits = subprocess.run(
        [
            "git",
            "rev-list",
            f"--max-count={_SOURCE_HISTORY_SCAN_LIMIT}",
            f"origin/{source}",
            "--",
            path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if commits.returncode != 0:
        return False

    shas = commits.stdout.split()
    for sha in shas:
        blob = subprocess.run(
            ["git", "rev-parse", f"{sha}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if blob.returncode == 0 and blob.stdout.strip() == wanted:
            return True

    if len(shas) == _SOURCE_HISTORY_SCAN_LIMIT:
        err_console.print(
            f"[yellow]Warning:[/yellow] stopped after "
            f"{_SOURCE_HISTORY_SCAN_LIMIT} commits scanning {source} history "
            f"for [bold]{path}[/bold]; treating as target-authored."
        )
    return False


def _source_deleted_path(source: str, path: str) -> bool:
    """Return True when *source*'s history contains a deletion of *path*.

    Distinguishes "source deliberately removed this file" from "source never
    had this file" — the latter being a target-owned artifact (release notes,
    target-only config) that must not be touched.

    ``--no-merges`` keeps the answer about a real authored deletion rather than
    a merge commit that happens to drop the path on its first-parent side.

    Unlike ``git diff``, ``git log`` does not apply rename detection by default,
    so a rename on source correctly registers here as a deletion of the old
    path.
    """
    result = subprocess.run(
        [
            "git",
            "log",
            "--max-count=1",
            "--no-merges",
            "--diff-filter=D",
            "--format=%H",
            source if source.startswith("origin/") else f"origin/{source}",
            "--",
            path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _resolve_pair(target: str | None, pairs: list[SyncPair]) -> SyncPair:
    """Return the SyncPair matching *target*, or the only pair when target is None."""
    if not pairs:
        err_console.print(
            "[red]Error:[/red] No sync pairs configured. "
            "Add [bold]scaffold.sync[/bold] entries to fraises.yaml:\n\n"
            "  scaffold:\n"
            "    sync:\n"
            "      - source: dev\n"
            "        target: staging\n"
        )
        raise SystemExit(1)

    if target is None:
        if len(pairs) == 1:
            return pairs[0]
        names = ", ".join(p.target for p in pairs)
        err_console.print(
            f"[red]Error:[/red] Multiple sync pairs configured — "
            f"specify a target. Available: {names}"
        )
        raise SystemExit(1)

    for pair in pairs:
        if pair.target == target:
            return pair

    names = ", ".join(p.target for p in pairs)
    err_console.print(
        f"[red]Error:[/red] No sync pair with target [bold]{target!r}[/bold]. "
        f"Available: {names}"
    )
    raise SystemExit(1)


def _read_branch_version(branch: str) -> str:
    """Read version string from version.json or pyproject.toml on *branch*."""
    r = subprocess.run(
        ["git", "show", f"origin/{branch}:version.json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode == 0:
        try:
            data = json.loads(r.stdout)
            v = data.get("version")
            if v:
                return str(v)
        except (json.JSONDecodeError, AttributeError):
            pass

    r2 = subprocess.run(
        ["git", "show", f"origin/{branch}:pyproject.toml"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r2.returncode == 0:
        for line in r2.stdout.splitlines():
            m = re.match(r'^version\s*=\s*"([^"]+)"', line)
            if m:
                return m.group(1)

    return "unknown"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True)


def _capture(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def _enable_auto_merge_or_merge_now(pr_url: str) -> None:
    """Enable auto-merge, falling back to immediate merge for #244.

    `gh pr merge --auto` fails on PRs in "clean" status (no required
    checks on the target branch). When that happens, merge now — there's
    nothing to auto-merge against anyway. Other gh failures propagate.
    """
    result = subprocess.run(
        ["gh", "pr", "merge", "--auto", "--squash", pr_url],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return
    stderr = result.stderr or ""
    if "enablePullRequestAutoMerge" in stderr or "clean status" in stderr:
        console.print(
            "[yellow]Target has no required checks; merging now instead of "
            "waiting on auto-merge.[/yellow]"
        )
        subprocess.run(["gh", "pr", "merge", "--squash", pr_url], check=True)
        return
    raise subprocess.CalledProcessError(
        result.returncode,
        result.args,
        output=result.stdout,
        stderr=result.stderr,
    )


# fraisier owns the `fraisier/**` branch namespace. Any branch under
# this prefix may be created, updated, or deleted by fraisier without
# warning — see README "Branch namespace". Code outside `fraisier/**`
# is treated as user-owned and never touched here.
FRAISIER_NS = "fraisier/"


def _sync_branch_name(target: str, source: str) -> str:
    return f"{FRAISIER_NS}sync/{target}-from-{source}"


def _remote_branch_exists(branch: str) -> bool:
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _is_non_fast_forward_rejection(exc: subprocess.CalledProcessError) -> bool:
    """True for git's 'fetch first' / non-fast-forward push rejection shape.

    The push subprocess runs under ``LC_ALL=C`` so this English match is
    reliable regardless of operator locale.
    """
    blob = (exc.stderr or "") + (exc.output or "")
    return "non-fast-forward" in blob or "fetch first" in blob


def _reclaim_orphan_branch_if_safe(branch: str) -> bool:
    """Delete ``origin/<branch>`` if its most recent PR is merged/closed.

    Returns True if the remote branch was deleted or never existed.
    Returns False if a live OPEN PR is still using it — caller should
    fall back to the "update existing PR" path.

    Refuses to operate outside ``FRAISIER_NS``: the namespace contract
    in the README is the only thing making unconditional deletion safe.
    """
    if not branch.startswith(FRAISIER_NS):
        raise ValueError(f"refusing to reclaim non-fraisier branch: {branch}")

    if not _remote_branch_exists(branch):
        return True

    existing = _find_existing_pr(branch)
    if existing is not None and existing["state"] == "OPEN":
        return False

    if existing is not None:
        console.print(
            f"  Reclaiming orphan {branch} "
            f"(prior PR {existing['state'].lower()}: {existing['url']})"
        )

    # check=False: the branch may have vanished between the exists-check
    # above and this delete (concurrent fraisier run, manual cleanup).
    # "Already gone" is the desired end state, not a failure.
    result = subprocess.run(
        ["git", "push", "origin", "--delete", branch],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 and "remote ref does not exist" not in (
        result.stderr or ""
    ):
        console.print(
            f"  [yellow]Warning: could not delete {branch}: "
            f"{(result.stderr or '').strip()}[/yellow]"
        )
    return True


def _push_sync_branch(sync_branch: str) -> None:
    """Push ``sync_branch`` to origin, recovering from #248 orphan branches.

    Flow:
      1. Pre-flight: if origin holds an orphan ``sync_branch`` whose most
         recent PR is MERGED/CLOSED, delete it first.
      2. Push. ``LC_ALL=C`` so the non-FF sniffer matches English stderr
         regardless of operator locale.
      3. On non-FF rejection, retry once: reclaim again (a PR may have
         merged in the gap), then either re-push or fall back to
         ``--force-with-lease`` if a live OPEN PR is still using the
         branch. Force-with-lease is safe here only because the branch
         lives in ``FRAISIER_NS`` (declared tool-owned in README).
      4. Any other failure (auth, network, etc.) propagates.
    """
    _reclaim_orphan_branch_if_safe(sync_branch)

    push_argv = ["git", "push", "origin", sync_branch]
    env = {**os.environ, "LC_ALL": "C"}
    result = subprocess.run(
        push_argv, capture_output=True, text=True, env=env, check=False
    )
    if result.returncode == 0:
        return

    exc = subprocess.CalledProcessError(
        result.returncode, push_argv, output=result.stdout, stderr=result.stderr
    )
    if not _is_non_fast_forward_rejection(exc):
        raise exc

    # Race window between pre-flight and push, or live OPEN PR.
    reclaimed = _reclaim_orphan_branch_if_safe(sync_branch)
    retry_argv = (
        push_argv
        if reclaimed
        else ["git", "push", "--force-with-lease", "origin", sync_branch]
    )
    retry = subprocess.run(
        retry_argv, capture_output=True, text=True, env=env, check=False
    )
    if retry.returncode != 0:
        raise subprocess.CalledProcessError(
            retry.returncode, retry_argv, output=retry.stdout, stderr=retry.stderr
        )


def _find_existing_pr(sync_branch: str) -> dict | None:
    """Return PR head-ref lookup result, or None if no PR exists for the branch.

    Result shape: ``{"url": str, "state": "OPEN" | "CLOSED" | "MERGED"}``.
    Uses ``check=False`` because ``gh`` exits non-zero when no PR matches
    the head ref — a normal "nothing here yet" signal, not an error.
    Drafts are reported as state OPEN (the ``isDraft`` bit lives in a
    separate field we don't query); re-enabling auto-merge on a draft is
    the desired behavior — GitHub queues the merge for when the PR is
    marked ready.
    """
    result = subprocess.run(
        ["gh", "pr", "view", sync_branch, "--json", "url,state"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def _merge_in_progress() -> bool:
    """Return True when MERGE_HEAD exists (a merge is mid-flight).

    Uses ``git rev-parse --verify`` so the check works under worktrees and
    in any cwd inside the repo, without manual ``.git`` path manipulation.
    """
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "MERGE_HEAD"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def _commit_merge_or_staged(message: str) -> None:
    """Commit a pending merge, or a non-merge staged index.

    A merge in progress is signalled by ``MERGE_HEAD``. In that case we
    always create the merge commit, even when the resolved tree matches
    HEAD byte-for-byte — git happily produces a merge commit with no
    tree diff, and that's exactly what records both parents and lets
    GitHub see the branch as merged. ``git merge --no-commit`` always
    leaves ``MERGE_HEAD`` set, so this fires for both clean merges and
    resolved-conflict paths.

    Outside a merge (no ``MERGE_HEAD``), fall back to "commit only if
    something is staged" so we don't error on an empty index.
    """
    cmd = ["git", "commit", "--no-edit", "--no-verify", "-m", message]
    in_merge = _merge_in_progress()
    if not in_merge:
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
        if staged.returncode == 0:
            return
    try:
        _run(cmd)
    except subprocess.CalledProcessError as exc:
        phase = "merge finalization" if in_merge else "pre-merge commit"
        err_console.print(
            "[red]✗ Sync abort:[/red] "
            f"`git commit` failed during {phase} (exit {exc.returncode}). "
            "Inspect `git status` to see what went wrong; common causes are "
            "a hostile pre-commit hook ignoring --no-verify or a corrupt index."
        )
        raise SystemExit(1) from exc


def _assert_clean_worktree() -> None:
    """Refuse to sync from a dirty worktree — before touching any branch.

    ``git checkout -B`` carries uncommitted modifications onto the sync
    branch, where the pre-merge commit would silently swallow them into
    the sync PR; untracked files can abort ``git merge`` before it even
    starts (#268). Abort with the file list instead. fraisier never
    cleans, stashes, or deletes operator files itself.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if status.returncode == 0 and not dirty:
        return
    err_console.print(
        "[red]✗ Sync abort:[/red] the working tree is not clean. "
        "Sync creates branches and merge commits here; uncommitted or "
        "untracked files could be swept into the sync PR or block the merge."
    )
    for line in dirty:
        err_console.print(f"    {line}", markup=False)
    if status.returncode != 0:
        err_console.print(f"    (git status failed: {status.stderr.strip()})")
    err_console.print(
        "  Commit, stash, or move these files, then re-run fraisier sync."
    )
    raise SystemExit(1)


def _abort_merge_never_started(merge_result: subprocess.CompletedProcess) -> None:
    """``git merge`` exited non-zero without starting a merge.

    No MERGE_HEAD and no unmerged paths means the merge refused to run at
    all — e.g. untracked or locally modified files would be overwritten
    (#268). There is nothing to resolve and nothing staged; falling
    through to the commit step would produce a single-parent HEAD and a
    misleading "fraisier bug" abort. Surface git's own diagnostics
    (previously swallowed by ``capture_output``) and stop.
    """
    err_console.print(
        "[red]✗ Sync abort:[/red] `git merge` failed before a merge started — "
        "there are no conflicts to auto-resolve. git reported:"
    )
    for stream in (merge_result.stdout, merge_result.stderr):
        for line in (stream or "").splitlines():
            err_console.print(f"    {line}", markup=False)
    err_console.print(
        "  Fix the reported problem (commit, stash, or move the listed "
        "files), then re-run fraisier sync."
    )
    raise SystemExit(1)


def _assert_merge_finalized(tgt: str) -> None:
    """Fail loudly if a sync push would leave the PR in a CONFLICTING state.

    The push-safety invariant is: ``origin/<tgt>`` must be an ancestor of
    HEAD (GitHub sees every target-side commit contained in the PR head)
    and MERGE_HEAD must be cleared. A finalized two-parent merge commit
    satisfies it; so does the "Already up to date" merge where the target
    is strictly behind the source and no merge commit exists (#268 —
    the previous ``len(parents) >= 2`` form rejected that safe state).
    Anything else would push a ref GitHub marks ``mergeable=CONFLICTING``;
    surface that locally so the operator doesn't walk away thinking it
    worked.
    """
    contains_target = (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", f"origin/{tgt}", "HEAD"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    in_merge_still = _merge_in_progress()
    if contains_target and not in_merge_still:
        return

    def _snap(cmd: list[str]) -> str:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=False
        ).stdout.strip()

    parents = _snap(["git", "log", "-1", "--pretty=%P", "HEAD"]).split()
    head = _snap(["git", "log", "-1", "--oneline"])
    porcelain = _snap(["git", "status", "--porcelain"])
    err_console.print(
        "[red]✗ Sync abort:[/red] pre-merge finished but pushing HEAD would "
        f"leave a CONFLICTING PR on GitHub (origin/{tgt} contained in HEAD: "
        f"{contains_target}, merge in progress: {in_merge_still})."
    )
    err_console.print(f"  HEAD: {head} (parents: {parents or '?'})", markup=False)
    if porcelain:
        err_console.print("  git status --porcelain:")
        for line in porcelain.splitlines():
            err_console.print(f"    {line}", markup=False)
    err_console.print(
        "  This is a fraisier bug; please file an issue with the output above."
    )
    raise SystemExit(1)


def _propagate_source_deletions(source: str, tgt: str) -> list[str]:
    """Propagate source-side deletions that target is only holding a stale copy of.

    ``git merge`` doesn't surface "source deleted X, target unchanged" as a
    UU-style conflict — it silently keeps target's copy. This pre-pass finds
    those files and mirrors the deletion with ``git rm``.

    Candidates are the ``D`` set from :func:`_diff_paths` — present on target,
    absent from source — not ``merge-base..origin/<source>`` as before. Sync
    PRs are squash-merged, so the merge-base never advances past the original
    fork point; a file created on source *after* that ancient base and later
    deleted there is absent at both ends of that range and was never listed,
    while target still carried a copy from an earlier squash-sync. The result
    was a file resurrecting on every single sync (#290).

    Two gates narrow the candidate set, both failing closed:

    1. source's history actually contains a deletion of the path — otherwise it
       is a target-owned file source never had, and must not be touched;
    2. target's blob for the path appears somewhere in source's history of it —
       proving target holds source-derived content rather than its own work.

    Only paths whose ``git rm`` actually succeeds are returned; this matters
    when a deletion can't be applied (submodule, sparse-checkout exclusion, …)
    — we don't lie to the operator log that resolution succeeded when the index
    is unchanged.
    """
    candidates = _diff_paths(tgt, source, "D")

    propagated: list[str] = []
    for path in candidates:
        in_index = (
            subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", path],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
        if not in_index:
            continue
        if not _source_deleted_path(source, path):
            # Target owns this file; source never had it.
            continue
        if not _target_blob_is_source_derived(source, tgt, path):
            err_console.print(
                f"[yellow]Warning:[/yellow] [bold]{path}[/bold] was deleted on "
                f"{source} but {tgt}'s copy is not source-derived — leaving it "
                f"in place for you to decide."
            )
            continue
        # -f is required, not defensive: the pre-merge has just staged this
        # file's *addition* (target has it, the source-based branch does not),
        # so a plain `git rm` refuses with "changes staged in the index" and
        # the deletion silently degrades to a warning.
        rm_result = subprocess.run(
            ["git", "rm", "-f", "--", path],
            capture_output=True,
            text=True,
            check=False,
        )
        if rm_result.returncode == 0:
            propagated.append(path)
        else:
            detail = rm_result.stderr.strip() or "git rm failed"
            err_console.print(
                f"[yellow]Warning:[/yellow] could not propagate deletion "
                f"of [bold]{path}[/bold]: {detail}"
            )
    return propagated


def _propagate_source_reverts(source: str, tgt: str) -> list[str]:
    """Restore source's content where the merge silently took target's stale copy.

    The other half of #290. ``_propagate_source_deletions`` handles source
    removing a file; this handles source *reverting* one.

    When source reverts a path to exactly its merge-base content, git's 3-way
    merge sees ``ours == base`` and resolves it as *take theirs* — with a zero
    exit code and no conflict, so the tier loop never sees it. Given a correct
    base that is right. Under squash promotion the base is the ancient fork
    point that never advances, so ``ours == base`` stops meaning "source never
    touched this" and starts meaning "source added it and then reverted it",
    while target still carries the promoted copy. The revert is lost on every
    sync.

    A revert to any *other* content leaves ``ours != base != theirs`` and does
    conflict, where tier 3 already resolves it. Only the exact return to base
    content is silent, which is why this half survived the deletion fix.

    Detection deliberately computes no merge-base — anchoring on one is what
    broke the deletion half. It asks what the merge actually did:

    1. ``origin/<tgt>`` and ``origin/<source>`` differ on the path;
    2. the merged **index** blob equals *target's* blob — git took theirs whole;
    3. target's blob is source-derived, the same gate the deletion pass uses.

    Gate 2 replaces a merge-base computation and excludes two classes for free:
    a conflicted path has no stage-0 entry, so ``git rev-parse :<path>`` fails
    and it falls through to the tier loop untouched; and a clean auto-merge of
    non-overlapping hunks produces a blob equal to neither side, so both changes
    survive.

    Only paths whose checkout actually succeeds are returned — the operator log
    must not claim a resolution the index does not reflect.
    """
    candidates = _diff_paths(tgt, source, "M")

    restored: list[str] = []
    for path in candidates:
        target_blob = subprocess.run(
            ["git", "rev-parse", f"origin/{tgt}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        merged_blob = subprocess.run(
            ["git", "rev-parse", f":{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if target_blob.returncode != 0 or merged_blob.returncode != 0:
            # No stage-0 entry means the path is still unmerged: a real
            # conflict, which the tier loop owns.
            continue
        if merged_blob.stdout.strip() != target_blob.stdout.strip():
            # The merge did not take target's side wholesale — either source
            # won or the hunks merged cleanly. Nothing was silently lost.
            continue
        if not _target_blob_is_source_derived(source, tgt, path):
            err_console.print(
                f"[yellow]Warning:[/yellow] [bold]{path}[/bold] differs on "
                f"{source} but {tgt}'s copy is not source-derived — keeping "
                f"{tgt}'s version for you to decide."
            )
            continue
        # `git checkout <tree-ish> -- <path>` updates the index as well as the
        # worktree, so no separate `git add` is needed to stage the restore.
        checkout = subprocess.run(
            ["git", "checkout", f"origin/{source}", "--", path],
            capture_output=True,
            text=True,
            check=False,
        )
        if checkout.returncode == 0:
            restored.append(path)
        else:
            detail = checkout.stderr.strip() or "git checkout failed"
            err_console.print(
                f"[yellow]Warning:[/yellow] could not restore "
                f"[bold]{path}[/bold] from {source}: {detail}"
            )
    return restored


def _print_dry_run_plan(source: str, tgt: str, sync_branch: str) -> None:
    """Print the shell commands that sync would execute, without running them."""
    auto_owned = ", ".join(_AUTO_RESOLVED)
    pr_title = f"Promote {source} \u2192 {tgt}"
    pr_body = f"Automated promotion of `{source}` \u2192 `{tgt}`."

    console.print(f"[cyan]DRY RUN:[/cyan] sync {source} \u2192 {tgt}")
    console.print()
    console.print("  Would run:")
    console.print("    git status --porcelain  # abort if the worktree is dirty")
    console.print(f"    git fetch origin {source} {tgt}")
    console.print(f"    git checkout -B {sync_branch} origin/{source}")
    console.print(f"    git merge origin/{tgt} --no-edit --no-commit")
    console.print(
        f"    # files present on {tgt} but deleted on {source} are 'git rm'-ed to"
        f" propagate the deletion, when {source}'s history shows the deletion and"
        f" {tgt}'s copy is source-derived;"
        f" files {source} reverted to their merge-base content — which the merge"
        f" silently resolves in {tgt}'s favour — are restored from {source} when"
        f" {tgt}'s copy is source-derived;"
        f" conflicts in [{auto_owned}] auto-resolved from {source};"
        f" conflicts where {tgt} holds source-derived content also resolved from"
        f" {source};"
        " others cause a hard failure unless --prefer-source is used"
    )
    console.print(
        f'    git commit --no-edit --no-verify -m "Pre-merge {tgt} into sync branch'
        ' (auto-resolved fraisier files)"'
    )
    console.print(
        f"    # pre-push guard: origin/{tgt} must be an ancestor of HEAD and"
        " MERGE_HEAD must be cleared, else abort"
    )
    console.print(f"    git push origin {sync_branch}")
    console.print(
        f'    gh pr create --title "{pr_title}" --body "{pr_body}"'
        f" --base {tgt} --head {sync_branch} --no-maintainer-edit"
    )
    console.print("    gh pr merge --auto --squash <PR URL>")
    console.print("    git checkout <original-branch>")


@main.command(name="sync")
@click.argument("target", required=False, default=None)
@click.option(
    "--list", "list_pairs", is_flag=True, help="List configured sync pairs and exit."
)
@click.option(
    "--check", is_flag=True, help="Show version diff only; no git operations."
)
@click.option(
    "--dry-run", is_flag=True, help="Print commands that would run; make no changes."
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="List every auto-resolved file instead of counting them by category.",
)
@click.option(
    "--prefer-source",
    is_flag=True,
    help=(
        "Resolve remaining conflicts by taking the source version. "
        "Fraisier-owned files and unchanged target files auto-resolve first; "
        "this flag handles anything left over."
    ),
)
@click.pass_context
def sync_cmd(
    ctx: click.Context,
    target: str | None,
    list_pairs: bool,
    check: bool,
    dry_run: bool,
    yes: bool,
    verbose: bool,
    prefer_source: bool,
) -> None:
    """Promote source → target via an auto-merged sync PR.

    Reads sync pairs from scaffold.sync in fraises.yaml. Creates a sync
    branch from the source, pre-merges the target (auto-resolving
    fraisier-owned files), pushes, and opens a squash PR with auto-merge.

    \b
    Examples:
        fraisier sync                      # when only one pair is configured
        fraisier sync staging              # target=staging
        fraisier sync --list               # show configured pairs
        fraisier sync staging --check      # version diff only, no git ops
        fraisier sync staging --dry-run    # print commands, make no changes
        fraisier sync staging --yes        # skip confirmation prompt
        fraisier sync staging --verbose    # name every auto-resolved file
        fraisier sync staging --prefer-source  # auto-resolve conflicts from source
    """
    config = require_config(ctx)
    pairs: list[SyncPair] = config.scaffold.sync

    if list_pairs:
        if not pairs:
            console.print("[yellow]No sync pairs configured in scaffold.sync.[/yellow]")
            return
        for p in pairs:
            console.print(f"  {p.source} → {p.target}")
        return

    pair = _resolve_pair(target, pairs)
    source = pair.source
    tgt = pair.target
    sync_branch = _sync_branch_name(tgt, source)

    if dry_run:
        _print_dry_run_plan(source, tgt, sync_branch)
        return

    if check:
        src_ver = _read_branch_version(source)
        tgt_ver = _read_branch_version(tgt)
        console.print(f"[bold]Version diff:[/bold] {tgt_ver} → {src_ver}")
        console.print(f"  source ([cyan]{source}[/cyan]):  {src_ver}")
        console.print(f"  target ([cyan]{tgt}[/cyan]): {tgt_ver}")
        return

    console.print(f"==> Syncing [cyan]{source}[/cyan] → [cyan]{tgt}[/cyan]")

    _assert_clean_worktree()

    original_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    _run(["git", "fetch", "origin", source, tgt])

    source_sha = _capture(["git", "rev-parse", f"origin/{source}"]).stdout.strip()
    merge_base = _capture(
        ["git", "merge-base", f"origin/{source}", f"origin/{tgt}"]
    ).stdout.strip()

    if merge_base == source_sha:
        from fraisier._output import success

        success("Already up to date — nothing to sync.")
        return

    src_ver = _read_branch_version(source)
    tgt_ver = _read_branch_version(tgt)
    console.print(f"  Version: [yellow]{tgt_ver}[/yellow] → [green]{src_ver}[/green]")

    if not yes:
        if not click.confirm("  Proceed with sync?"):
            console.print("  Aborted.")
            return

    branch_created = False
    try:
        console.print(f"  Creating [bold]{sync_branch}[/bold] from origin/{source}")
        _run(["git", "checkout", "-B", sync_branch, f"origin/{source}"])
        branch_created = True

        console.print(f"  Pre-merging origin/{tgt} into {sync_branch}")
        merge_result = subprocess.run(
            ["git", "merge", f"origin/{tgt}", "--no-edit", "--no-commit"],
            capture_output=True,
            text=True,
            check=False,
        )

        resolutions = _AutoResolutions(verbose=verbose, source=source, target=tgt)

        # Runs unconditionally because source-side deletions don't show up
        # as `UU` conflicts — `git merge` silently keeps target's copy
        # whether the merge as a whole was clean or had unrelated conflicts.
        # See #235. By running before the conflict loop, surviving
        # source-deleted-target-modified files still flow through tier 1.
        for deleted in _propagate_source_deletions(source, tgt):
            resolutions.record(_SOURCE_DELETION, deleted)

        # The other half of #290, and unconditional for the same reason: a
        # source-side revert to base content merges *cleanly* and takes
        # target's stale copy, so it never reaches the conflict loop below.
        for restored in _propagate_source_reverts(source, tgt):
            resolutions.record(_SOURCE_REVERT, restored)

        if merge_result.returncode != 0:
            conflicted = _capture(
                ["git", "diff", "--name-only", "--diff-filter=U"]
            ).stdout.splitlines()

            # Non-zero exit with no unmerged paths and no MERGE_HEAD means
            # the merge never started (#268) — a different failure class
            # than conflicts, with nothing to resolve or commit.
            if not any(f.strip() for f in conflicted) and not _merge_in_progress():
                _abort_merge_never_started(merge_result)

            for raw_f in conflicted:
                f = raw_f.strip()
                if not f:
                    continue
                exists_in_source = (
                    subprocess.run(
                        ["git", "cat-file", "-e", f"origin/{source}:{f}"],
                        capture_output=True,
                        check=False,
                    ).returncode
                    == 0
                )
                if not exists_in_source:
                    subprocess.run(["git", "rm", f], capture_output=True, check=False)
                    resolutions.record(_SOURCE_DELETION, f)
                elif _is_auto_resolved(f):
                    # Tier 1: fraisier-owned
                    subprocess.run(
                        ["git", "checkout", f"origin/{source}", "--", f],
                        capture_output=True,
                        check=False,
                    )
                    subprocess.run(["git", "add", f], capture_output=True, check=False)
                    resolutions.record(_FRAISIER_OWNED, f)
                elif _target_blob_is_source_derived(source, tgt, f):
                    # Tier 3: target is holding a stale copy of source's own
                    # content, so taking source's side loses nothing. Asked as
                    # "is this blob source-derived?" rather than "unchanged
                    # since merge-base" — under squash promotion the merge-base
                    # is permanently stale, which made this tier almost never
                    # fire and pushed resolvable conflicts to a tier-5 abort.
                    subprocess.run(
                        ["git", "checkout", f"origin/{source}", "--", f],
                        capture_output=True,
                        check=False,
                    )
                    subprocess.run(["git", "add", f], capture_output=True, check=False)
                    resolutions.record(_STALE_TARGET, f)
                elif prefer_source or pair.prefer_source:
                    # Tier 4: explicit preference — source wins
                    subprocess.run(
                        ["git", "checkout", f"origin/{source}", "--", f],
                        capture_output=True,
                        check=False,
                    )
                    subprocess.run(["git", "add", f], capture_output=True, check=False)
                    resolutions.record(_PREFER_SOURCE, f)

            resolutions.summarise()

            remaining = _capture(
                ["git", "diff", "--name-only", "--diff-filter=U"]
            ).stdout.strip()

            if remaining:
                err_console.print(
                    "[red]✗ Unresolved conflicts in non-fraisier-owned files:[/red]"
                )
                for line in remaining.splitlines():
                    err_console.print(f"    {line}")
                err_console.print(
                    "  Resolve these manually before running fraisier sync."
                )
                raise SystemExit(1)

            _commit_merge_or_staged(
                f"Pre-merge {tgt} into sync branch (auto-resolved fraisier files)"
            )
        else:
            resolutions.summarise()
            _commit_merge_or_staged(f"Pre-merge {tgt} into sync branch")

        _assert_merge_finalized(tgt)

        console.print(f"  Pushing [bold]{sync_branch}[/bold]")
        _push_sync_branch(sync_branch)

        existing = _find_existing_pr(sync_branch)
        if existing and existing["state"] == "OPEN":
            pr_url = existing["url"]
            console.print(f"  Existing open PR found, updating: [bold]{pr_url}[/bold]")
            _enable_auto_merge_or_merge_now(pr_url)
            subprocess.run(["git", "checkout", original_branch], check=False)
            from fraisier._output import success

            success(f"Done. PR updated and auto-merge enabled: {pr_url}")
            return
        if existing:
            console.print(
                f"  Prior PR for {sync_branch} was {existing['state'].lower()}: "
                f"{existing['url']} — opening a new one"
            )

        console.print(f"  Creating PR: [bold]{sync_branch}[/bold] → [bold]{tgt}[/bold]")
        pr_result = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--title",
                f"Promote {source} → {tgt}",
                "--body",
                f"Automated promotion of `{source}` → `{tgt}`.",
                "--base",
                tgt,
                "--head",
                sync_branch,
                "--no-maintainer-edit",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        pr_url = pr_result.stdout.strip()

        _enable_auto_merge_or_merge_now(pr_url)

        subprocess.run(["git", "checkout", original_branch], check=False)
        from fraisier._output import success

        success(f"Done. PR created and auto-merge enabled: {pr_url}")

    except SystemExit:
        subprocess.run(["git", "checkout", original_branch], check=False)
        if branch_created:
            subprocess.run(["git", "branch", "-D", sync_branch], check=False)
        raise

    except Exception as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        subprocess.run(["git", "checkout", original_branch], check=False)
        if branch_created:
            subprocess.run(["git", "branch", "-D", sync_branch], check=False)
        raise SystemExit(1) from exc

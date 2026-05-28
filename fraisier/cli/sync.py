"""sync command — promote source branch → target branch via a sync PR."""

from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING

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


def _is_auto_resolved(path: str) -> bool:
    """Return True if *path* is a fraisier-owned file that can be auto-resolved."""
    for pattern in _AUTO_RESOLVED:
        if path == pattern or path.startswith(pattern + "/"):
            return True
    return False


def _target_unchanged_since_base(merge_base: str, tgt: str, path: str) -> bool:
    """Return True if *path* in origin/{tgt} is identical to the merge-base version.

    When True, the target branch never touched this file — only the source did.
    It is safe to auto-resolve by taking the source version.

    Reads from refs (``merge_base`` and ``origin/{tgt}``), not the index, so
    the answer is meaningful mid-conflict — callers can ask "did target ever
    touch this file?" without worrying about whatever transient stage-1/2/3
    state ``git merge`` left behind.

    Any non-zero exit — including unexpected git errors — returns False, which
    causes the file to fall through to tier 5 (abort). This is intentional: we
    would rather fail loudly than silently claim a file is unmodified.
    """
    result = subprocess.run(
        ["git", "diff", "--quiet", merge_base, f"origin/{tgt}", "--", path],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


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


def _assert_merge_finalized() -> None:
    """Fail loudly if a sync push would leave the PR in a CONFLICTING state.

    After all commit attempts and before pushing, HEAD must be a merge
    commit (at least two parents — octopus merges with ≥3 are valid too)
    and MERGE_HEAD must be cleared. If either invariant is broken, the
    push would silently drop the merge parent and GitHub would mark the
    PR ``mergeable=CONFLICTING``. Surface that locally so the operator
    doesn't walk away thinking it worked.
    """
    parents = subprocess.run(
        ["git", "log", "-1", "--pretty=%P", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    in_merge_still = _merge_in_progress()
    if in_merge_still or not parents or len(parents) < 2:
        err_console.print(
            "[red]✗ Sync abort:[/red] auto-resolve completed but HEAD is not "
            "a merge commit "
            f"(parents: {parents or '?'}, merge in progress: {in_merge_still}).\n"
            "  Pushing now would leave a CONFLICTING PR on GitHub. "
            "This is a fraisier bug; please file an issue with the output above."
        )
        raise SystemExit(1)


def _propagate_source_deletions(merge_base: str, source: str, tgt: str) -> list[str]:
    """Propagate source-side deletions that target didn't touch since merge-base.

    ``git merge`` doesn't surface "source deleted X, target unchanged" as
    a UU-style conflict — it silently keeps target's copy. This pre-pass
    walks files deleted on source since merge-base; for each one still
    in the index where target hasn't modified it, run ``git rm`` to
    mirror the source-side deletion.

    Files that target *did* modify since merge-base are left alone — the
    operator (or the conflict loop) decides, and the existing tier-1
    auto-resolver already handles the "source deleted, target modified"
    conflict case via ``cat-file -e``.

    Only paths whose ``git rm`` actually succeeds are returned; this
    matters when a deletion can't be applied (submodule, sparse-checkout
    exclusion, …) — we don't lie to the operator log that resolution
    succeeded when the index is unchanged.
    """
    deleted_on_source = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=D",
            merge_base,
            f"origin/{source}",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()

    propagated: list[str] = []
    for raw in deleted_on_source:
        path = raw.strip()
        if not path:
            continue
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
        if _target_unchanged_since_base(merge_base, tgt, path):
            rm_result = subprocess.run(
                ["git", "rm", "--", path],
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


def _print_dry_run_plan(source: str, tgt: str, sync_branch: str) -> None:
    """Print the shell commands that sync would execute, without running them."""
    auto_owned = ", ".join(_AUTO_RESOLVED)
    pr_title = f"Promote {source} \u2192 {tgt}"
    pr_body = f"Automated promotion of `{source}` \u2192 `{tgt}`."

    console.print(f"[cyan]DRY RUN:[/cyan] sync {source} \u2192 {tgt}")
    console.print()
    console.print("  Would run:")
    console.print(f"    git fetch origin {source} {tgt}")
    console.print(f"    git checkout -B {sync_branch} origin/{source}")
    console.print(f"    git merge origin/{tgt} --no-edit --no-commit")
    console.print(
        f"    # files deleted on {source} with {tgt} unchanged since merge-base"
        f" are 'git rm'-ed to propagate the deletion;"
        f" conflicts in [{auto_owned}] auto-resolved from {source};"
        f" files unchanged in {tgt} since merge-base also auto-resolved from {source};"
        " others cause a hard failure unless --prefer-source is used"
    )
    console.print(
        f'    git commit --no-edit --no-verify -m "Pre-merge {tgt} into sync branch'
        ' (auto-resolved fraisier files)"'
    )
    console.print(
        "    # pre-push guard: HEAD must be a merge commit (two parents) and"
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
    sync_branch = f"sync/{tgt}-from-{source}"

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
        console.print("  [green]Already up to date — nothing to sync.[/green]")
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

        # Runs unconditionally because source-side deletions don't show up
        # as `UU` conflicts — `git merge` silently keeps target's copy
        # whether the merge as a whole was clean or had unrelated conflicts.
        # See #235. By running before the conflict loop, surviving
        # source-deleted-target-modified files still flow through tier 1.
        for deleted in _propagate_source_deletions(merge_base, source, tgt):
            console.print(f"  Auto-resolved (source deletion): {deleted}")

        if merge_result.returncode != 0:
            conflicted = _capture(
                ["git", "diff", "--name-only", "--diff-filter=U"]
            ).stdout.splitlines()

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
                    console.print(f"  Auto-resolved source deletion: {f}")
                elif _is_auto_resolved(f):
                    # Tier 1: fraisier-owned
                    subprocess.run(
                        ["git", "checkout", f"origin/{source}", "--", f],
                        capture_output=True,
                        check=False,
                    )
                    subprocess.run(["git", "add", f], capture_output=True, check=False)
                elif _target_unchanged_since_base(merge_base, tgt, f):
                    # Tier 3: target hasn't changed file since merge-base
                    subprocess.run(
                        ["git", "checkout", f"origin/{source}", "--", f],
                        capture_output=True,
                        check=False,
                    )
                    subprocess.run(["git", "add", f], capture_output=True, check=False)
                    console.print(
                        f"  Auto-resolved ({tgt} unchanged since merge-base): {f}"
                    )
                elif prefer_source or pair.prefer_source:
                    # Tier 4: explicit preference — source wins
                    subprocess.run(
                        ["git", "checkout", f"origin/{source}", "--", f],
                        capture_output=True,
                        check=False,
                    )
                    subprocess.run(["git", "add", f], capture_output=True, check=False)
                    console.print(f"  Auto-resolved (prefer-source): {f}")

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
            _commit_merge_or_staged(f"Pre-merge {tgt} into sync branch")

        _assert_merge_finalized()

        console.print(f"  Pushing [bold]{sync_branch}[/bold]")
        _run(["git", "push", "origin", sync_branch])

        existing = _find_existing_pr(sync_branch)
        if existing and existing["state"] == "OPEN":
            pr_url = existing["url"]
            console.print(f"  Existing open PR found, updating: [bold]{pr_url}[/bold]")
            _run(["gh", "pr", "merge", "--auto", "--squash", pr_url])
            subprocess.run(["git", "checkout", original_branch], check=False)
            console.print(
                f"==> [green]Done.[/green] PR updated and auto-merge enabled: {pr_url}"
            )
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

        _run(["gh", "pr", "merge", "--auto", "--squash", pr_url])

        subprocess.run(["git", "checkout", original_branch], check=False)
        console.print(
            f"==> [green]Done.[/green] PR created and auto-merge enabled: {pr_url}"
        )

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

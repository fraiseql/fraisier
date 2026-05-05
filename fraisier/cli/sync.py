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


def _print_dry_run_plan(source: str, tgt: str, sync_branch: str) -> None:
    """Print the shell commands that sync would execute, without running them."""
    auto_owned = ", ".join(_AUTO_RESOLVED)
    pr_title = f"Promote {source} \u2192 {tgt}"
    pr_body = f"Automated promotion of `{source}` \u2192 `{tgt}`."

    console.print(f"[cyan]DRY RUN:[/cyan] sync {source} \u2192 {tgt}")
    console.print()
    console.print("  Would run:")
    console.print(f"    git fetch origin {source} {tgt}")
    console.print(f"    git checkout -b {sync_branch} origin/{source}")
    console.print(f"    git merge origin/{tgt} --no-edit --no-commit")
    console.print(
        f"    # conflicts in [{auto_owned}] auto-resolved from {source};"
        f" files unchanged in {tgt} since merge-base also auto-resolved from {source};"
        " others cause a hard failure unless --prefer-source is used"
    )
    console.print(
        f'    git commit --no-edit --no-verify -m "Pre-merge {tgt} into sync branch'
        ' (auto-resolved fraisier files)"'
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
        _run(["git", "checkout", "-b", sync_branch, f"origin/{source}"])
        branch_created = True

        console.print(f"  Pre-merging origin/{tgt} into {sync_branch}")
        merge_result = subprocess.run(
            ["git", "merge", f"origin/{tgt}", "--no-edit", "--no-commit"],
            capture_output=True,
            text=True,
            check=False,
        )

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

            _run(
                [
                    "git",
                    "commit",
                    "--no-edit",
                    "--no-verify",
                    "-m",
                    f"Pre-merge {tgt} into sync branch (auto-resolved fraisier files)",
                ]
            )
        else:
            # Clean merge — commit only if something was staged.
            staged = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
            if staged.returncode != 0:
                _run(
                    [
                        "git",
                        "commit",
                        "--no-edit",
                        "--no-verify",
                        "-m",
                        f"Pre-merge {tgt} into sync branch",
                    ]
                )

        console.print(f"  Pushing [bold]{sync_branch}[/bold]")
        _run(["git", "push", "origin", sync_branch])

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

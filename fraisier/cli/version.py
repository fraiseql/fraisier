"""Version management commands (version show, version bump, ship)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import click

from fraisier._output import success

from ._helpers import console
from .main import main

if TYPE_CHECKING:
    from fraisier.config import ShipConfig

logger = logging.getLogger(__name__)


def _get_systemd_version() -> str:
    """Get systemd version string, or 'Not detected' if unavailable."""
    try:
        import subprocess

        result = subprocess.run(
            ["systemctl", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            # First line is like "systemd 249 (249.7-1-arch)"
            return result.stdout.split("\n")[0]
    except (subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "Not detected"


def _show_full_version(fraisier_version: str) -> None:
    """Display detailed version and system information."""
    import platform
    import sys

    console.print(f"[bold]Fraisier[/bold] v{fraisier_version}")
    console.print(
        f"[bold]Python[/bold]   {sys.version.split()[0]} ({platform.platform()})"
    )
    console.print(f"[bold]Systemd[/bold]  {_get_systemd_version()}")


@main.group(name="version", invoke_without_command=True)
@click.option("--full", is_flag=True, help="Show detailed system information")
@click.pass_context
def version_group(ctx: click.Context, full: bool) -> None:
    """Version management commands.

    \b
    Without subcommand: show Fraisier package version.
    With subcommand: manage project version.json.

    \b
    Examples:
        fraisier version            # Show package version
        fraisier version --full     # Show system details
        fraisier version show       # Show version.json info
        fraisier version bump patch # Bump patch version
    """
    if ctx.invoked_subcommand is None:
        from fraisier import __version__

        if full:
            _show_full_version(__version__)
        else:
            console.print(f"Fraisier v{__version__}")


@version_group.command(name="show")
@click.option(
    "--version-file",
    "-f",
    default="version.json",
    help="Path to version.json",
)
def version_show(version_file: str) -> None:
    """Show project version info from version.json.

    \b
    Examples:
        fraisier version show
        fraisier version show --version-file dist/version.json
    """
    from fraisier.versioning import read_version

    info = read_version(Path(version_file))
    if info is None:
        console.print(f"[red]Error:[/red] Version file not found: {version_file}")
        raise SystemExit(1)

    console.print(f"[bold]Version:[/bold]          {info.version}")
    if info.commit:
        console.print(f"[bold]Commit:[/bold]           {info.commit}")
    if info.branch:
        console.print(f"[bold]Branch:[/bold]           {info.branch}")
    if info.timestamp:
        console.print(f"[bold]Timestamp:[/bold]        {info.timestamp}")
    if info.environment:
        console.print(f"[bold]Environment:[/bold]      {info.environment}")
    if info.schema_hash:
        console.print(f"[bold]Schema Hash:[/bold]      {info.schema_hash}")
    if info.database_version:
        console.print(f"[bold]Database Version:[/bold] {info.database_version}")


@version_group.command(name="bump")
@click.argument("part", type=click.Choice(["major", "minor", "patch"]))
@click.option(
    "--pyproject",
    "-f",
    default="pyproject.toml",
    help="Path to pyproject.toml",
)
@click.option("--dry-run", is_flag=True, help="Show what would change")
@click.option("--no-tag", is_flag=True, help="Skip git tag creation")
def version_bump(
    part: str,
    pyproject: str,
    dry_run: bool,
    no_tag: bool,  # noqa: ARG001
) -> None:
    """Bump project version in pyproject.toml (major, minor, or patch).

    \b
    Examples:
        fraisier version bump patch
        fraisier version bump minor --dry-run
        fraisier version bump major --no-tag
    """
    from fraisier.versioning import bump_version, parse_semver

    path = Path(pyproject)
    old_version = _read_current_version(path)
    major, minor, patch_v = parse_semver(old_version)

    if part == "major":
        major += 1
        minor = 0
        patch_v = 0
    elif part == "minor":
        minor += 1
        patch_v = 0
    else:
        patch_v += 1

    new_version = f"{major}.{minor}.{patch_v}"

    if dry_run:
        console.print(f"[cyan]DRY RUN:[/cyan] {old_version} -> {new_version}")
        return

    result = bump_version(path, part)
    console.print(f"[green]Bumped:[/green] {old_version} -> {result.version}")


@main.command(name="ship")
@click.argument(
    "bump_type",
    type=click.Choice(["patch", "minor", "major"]),
    required=False,
    default=None,
)
@click.option("--no-bump", is_flag=True, help="Skip version bump")
@click.option("--dry-run", is_flag=True, help="Show what would happen")
@click.option("--no-deploy", is_flag=True, help="Skip deploy after push")
@click.option("--pr", "create_pr", is_flag=True, help="Create a PR after push")
@click.option("--pr-base", default=None, help="Base branch for the PR")
@click.option(
    "--pyproject",
    type=click.Path(),
    default="pyproject.toml",
    help="Path to pyproject.toml (single source of truth for version)",
)
@click.option(
    "--wait-deploy",
    is_flag=True,
    help="Wait for deployment to complete and verify via health check",
)
@click.option(
    "--deploy-timeout",
    type=int,
    default=300,
    help="Timeout in seconds for deployment verification",
)
@click.option(
    "--auto-merge",
    "auto_merge",
    is_flag=True,
    help=(
        "Enable GitHub auto-merge on the PR after push. "
        "Requires an existing PR; combine with --pr to create one."
    ),
)
@click.option(
    "--merge-method",
    type=click.Choice(["squash", "merge", "rebase"]),
    default=None,
    help=(
        "Merge method for auto-merge (squash/merge/rebase). "
        "Falls back to ship.merge_method in fraises.yaml."
    ),
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text). JSON currently supported on --dry-run only.",
)
@click.pass_context
def ship(
    ctx: click.Context,
    bump_type: str | None,
    no_bump: bool,
    dry_run: bool,
    no_deploy: bool,
    create_pr: bool,
    pr_base: str | None,
    pyproject: str,
    wait_deploy: bool,
    deploy_timeout: int,
    auto_merge: bool,
    merge_method: str | None,
    fmt: str,
) -> None:
    """Bump version, commit, push, and deploy in one step.

    \b
    Examples:
        fraisier ship patch
        fraisier ship minor --dry-run
        fraisier ship patch --no-deploy
        fraisier ship patch --pr --pr-base dev
        fraisier ship --no-bump
        fraisier ship minor --auto-merge
        fraisier ship patch --pr --auto-merge --merge-method rebase

    \b
    Flag interactions:
        --pr               Create a PR after push. Requires --pr-base
                           (or ship.pr_base in fraises.yaml).
        --auto-merge       Enable GitHub auto-merge. Requires either
                           --pr (to create the PR first) or an existing
                           PR for HEAD.
        --wait-deploy      Block until the deploy webhook reports
                           success. Implies --auto-merge when paired
                           with --pr — it won't return until the PR is
                           merged AND the resulting deploy lands.
        --no-deploy        Skip the deploy step entirely; useful for
                           tag-only releases.
        --no-bump          Re-ship the current version without bumping.
                           Cannot combine with a bump-type argument.
    """
    if no_bump and bump_type is not None:
        console.print(
            "[red]Error:[/red] Cannot use --no-bump with a bump type argument"
        )
        raise SystemExit(1)
    if not no_bump and bump_type is None:
        console.print(
            "[red]Error:[/red] Bump type (patch, minor, major) is required "
            "unless --no-bump is set"
        )
        raise SystemExit(1)

    from fraisier.versioning import bump_version

    pyproject_path = Path(pyproject)
    current_version = _read_current_version(pyproject_path)

    # Resolve ship config (may be None if no fraises.yaml)
    config = ctx.obj.get("config") if ctx.obj else None
    ship_config: ShipConfig | None = config.ship if config else None

    # CLI flags take precedence; fall back to fraises.yaml ship: section defaults
    resolved_auto_merge = auto_merge or (
        ship_config.auto_merge if ship_config else False
    )
    resolved_merge_method = merge_method or (
        ship_config.merge_method if ship_config else "squash"
    )

    if no_bump:
        if dry_run:
            _ship_dry_run_no_bump(
                current_version,
                pyproject_path,
                ship_config,
                create_pr,
                pr_base,
                no_deploy,
                auto_merge=resolved_auto_merge,
                merge_method=resolved_merge_method,
                bare_repo_skip=_resolve_bare_repo_skip() if not no_deploy else None,
            )
            return

        _ship_commit_push_deploy(
            current_version,
            ship_config,
            create_pr,
            pr_base,
            no_deploy,
            wait_deploy,
            deploy_timeout,
            auto_merge=resolved_auto_merge,
            merge_method=resolved_merge_method,
            label=f"v{current_version} (no bump)",
            expected_base_version=current_version,
            bump_kind=None,
        )
        return

    new = _calc_new_version(current_version, bump_type)

    if dry_run and fmt == "json":
        import json as _json

        click.echo(
            _json.dumps(
                {
                    "version": {
                        "old": current_version,
                        "new": new,
                        "bump_type": bump_type,
                    },
                    "dry_run": True,
                    "create_pr": create_pr,
                    "pr_base": pr_base,
                    "no_deploy": no_deploy,
                    "auto_merge": resolved_auto_merge,
                    "merge_method": resolved_merge_method,
                },
                indent=2,
            )
        )
        return

    if dry_run:
        _ship_dry_run(
            current_version,
            new,
            bump_type,
            pyproject_path,
            ship_config,
            create_pr,
            pr_base,
            no_deploy,
            auto_merge=resolved_auto_merge,
            merge_method=resolved_merge_method,
            bare_repo_skip=_resolve_bare_repo_skip() if not no_deploy else None,
        )
        return

    assert bump_type is not None  # guaranteed by the CLI argument validation above
    info = bump_version(pyproject_path, bump_type)
    success(f"Version bumped: {current_version} -> {info.version}")

    _ship_commit_push_deploy(
        info.version,
        ship_config,
        create_pr,
        pr_base,
        no_deploy,
        wait_deploy,
        deploy_timeout,
        auto_merge=resolved_auto_merge,
        merge_method=resolved_merge_method,
        label=f"v{info.version}",
        expected_base_version=current_version,
        bump_kind=bump_type,
    )


def _ship_commit_push_deploy(
    version: str,
    ship_config: ShipConfig | None,
    create_pr: bool,
    pr_base: str | None,
    no_deploy: bool,
    wait_deploy: bool,
    deploy_timeout: int,
    *,
    auto_merge: bool = False,
    merge_method: str = "squash",
    label: str,
    expected_base_version: str,
    bump_kind: str | None,
) -> None:
    """Run the commit-push-PR-deploy sequence."""
    # #232: race base is the PR target when --pr is set (origin/<pr_base> moves
    # while we run CI), else the current branch (operator pushes the current
    # branch and the webhook reads pyproject.toml from it).
    resolved_pr_base = pr_base or (ship_config.pr_base if ship_config else None)
    race_base = resolved_pr_base if create_pr else None

    _ship_with_pipeline(
        version, ship_config, expected_base_version, bump_kind, race_base
    )

    if create_pr:
        pr_url = _ship_create_pr(version, pr_base, ship_config)
        if auto_merge and pr_url:
            _ship_enable_auto_merge(merge_method, pr_url=pr_url)
    elif auto_merge:
        _ship_enable_auto_merge(merge_method)

    success(f"Shipped {label}")

    if not no_deploy:
        _trigger_deploy_for_current_branch(
            wait_deploy, deploy_timeout, no_bump=bump_kind is None
        )


def _read_current_version(pyproject_path: Path) -> str:
    """Read and return the current version string from pyproject.toml."""
    from fraisier.versioning import read_pyproject_version

    try:
        return read_pyproject_version(pyproject_path)
    except FileNotFoundError:
        console.print(f"[red]Error:[/red] {pyproject_path} not found")
        raise SystemExit(1) from None
    except ValueError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise SystemExit(1) from None


def _calc_new_version(current_version: str, bump_type: str | None) -> str:
    """Calculate the new version string."""
    from fraisier.versioning import parse_semver

    major, minor, patch_v = parse_semver(current_version)
    if bump_type == "major":
        return f"{major + 1}.0.0"
    if bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch_v + 1}"


def _ship_dry_run(
    current_version: str,
    new: str,
    bump_type: str | None,
    pyproject_path: Path,
    ship_config: ShipConfig | None,
    create_pr: bool,
    pr_base: str | None,
    no_deploy: bool,
    auto_merge: bool = False,
    merge_method: str = "squash",
    bare_repo_skip: Path | None = None,
) -> None:
    """Print dry-run plan for ship."""
    console.print(f"[cyan]DRY RUN:[/cyan] Would ship v{new}")
    console.print(f"  Bump: {current_version} -> {new} ({bump_type})")
    console.print(f"  File: {pyproject_path}")
    if ship_config and ship_config.checks:
        console.print("  Pipeline checks:")
        for c in ship_config.checks:
            console.print(f"    [{c.phase}] {c.name}: {' '.join(c.command)}")
    console.print("  Git: add, commit, push")
    if create_pr:
        base = pr_base or (ship_config.pr_base if ship_config else None)
        console.print(f"  PR: create against {base or '<default branch>'}")
    if auto_merge:
        console.print(f"  Auto-merge: enable ({merge_method})")
    if not no_deploy:
        _print_deploy_note(bare_repo_skip)


def _ship_dry_run_no_bump(
    current_version: str,
    pyproject_path: Path,
    ship_config: ShipConfig | None,
    create_pr: bool,
    pr_base: str | None,
    no_deploy: bool,
    auto_merge: bool = False,
    merge_method: str = "squash",
    bare_repo_skip: Path | None = None,
) -> None:
    """Print dry-run plan for ship --no-bump."""
    console.print(f"[cyan]DRY RUN:[/cyan] Would ship v{current_version} (no bump)")
    console.print(f"  Version: {current_version} (unchanged)")
    console.print(f"  File: {pyproject_path}")
    if ship_config and ship_config.checks:
        console.print("  Pipeline checks:")
        for c in ship_config.checks:
            console.print(f"    [{c.phase}] {c.name}: {' '.join(c.command)}")
    console.print("  Git: add, commit, push")
    if create_pr:
        base = pr_base or (ship_config.pr_base if ship_config else None)
        console.print(f"  PR: create against {base or '<default branch>'}")
    if auto_merge:
        console.print(f"  Auto-merge: enable ({merge_method})")
    if not no_deploy:
        _print_deploy_note(bare_repo_skip)


def _print_deploy_note(bare_repo_skip: Path | None) -> None:
    if bare_repo_skip:
        console.print(
            f"  Deploy: skip — bare repo {bare_repo_skip} not found"
            f" (webhook will deploy)"
        )
    else:
        console.print("  Deploy: trigger for branch-mapped fraises")


def _ship_create_pr(
    version: str,
    pr_base: str | None,
    ship_config: ShipConfig | None,
) -> str | None:
    """Create a PR after push. Returns the PR URL on success, None otherwise."""
    base = pr_base or (ship_config.pr_base if ship_config else None)
    if not base:
        console.print(
            "[red]Error:[/red] --pr-base required (or set ship.pr_base in fraises.yaml)"
        )
        raise SystemExit(1)
    from fraisier.ship.pr import create_pr as do_create_pr

    return do_create_pr(version, base, console)


def _ship_enable_auto_merge(merge_method: str, *, pr_url: str | None = None) -> None:
    """Enable auto-merge on the current PR."""
    from fraisier.ship.pr import enable_auto_merge

    enable_auto_merge(merge_method, console, pr_url=pr_url)


def _commit_release(version: str) -> None:
    """Create the `release: v{version}` commit, surviving spurious non-zero exits.

    git commit occasionally exits non-zero (e.g. transient gpg-agent
    stderr) *after* writing a valid commit object. We capture HEAD
    before the attempt; on non-zero exit, if HEAD now points at a fresh
    commit whose subject is `release: v{version}`, the commit landed and
    we proceed. Otherwise we surface git's stderr to err_console and
    re-raise. See issue #243.
    """
    import subprocess

    from ._helpers import err_console

    expected_subject = f"release: v{version}"

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    head_before_hash = head_before.stdout.strip() if head_before.returncode == 0 else ""

    result = subprocess.run(
        ["git", "commit", "--no-verify", "-m", expected_subject],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    head_subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        check=False,
    )
    head_after_hash = head_after.stdout.strip() if head_after.returncode == 0 else ""
    landed = (
        head_after.returncode == 0
        and head_subject.returncode == 0
        and head_after_hash not in ("", head_before_hash)
        and head_subject.stdout.strip() == expected_subject
    )
    if landed:
        stderr_blurb = (result.stderr or "").strip()
        console.print(
            f"[yellow]git commit exited {result.returncode} but the release "
            "commit is present on HEAD (hash advanced). Proceeding.[/yellow]"
        )
        if stderr_blurb:
            console.print(f"[yellow]git stderr was:[/yellow]\n{stderr_blurb}")
        return

    stderr_blurb = (result.stderr or "").strip()
    stdout_blurb = (result.stdout or "").strip()
    err_console.print("[red]git commit failed and HEAD did not advance.[/red]")
    if stderr_blurb:
        err_console.print(f"[red]stderr:[/red]\n{stderr_blurb}")
    if stdout_blurb:
        err_console.print(f"[red]stdout:[/red]\n{stdout_blurb}")
    raise subprocess.CalledProcessError(
        result.returncode,
        result.args,
        output=result.stdout,
        stderr=result.stderr,
    )


def _git_push() -> None:
    """Push to remote, setting upstream if needed (#45)."""
    import subprocess

    # Check if current branch has a remote tracking branch
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # No upstream configured — push with --set-upstream
        subprocess.run(["git", "push", "-u", "origin", "HEAD"], check=True)
    else:
        subprocess.run(["git", "push"], check=True)


def _ship_with_pipeline(
    version: str,
    ship_config: ShipConfig | None,
    expected_base_version: str,
    bump_kind: str | None,
    race_base: str | None,
) -> None:
    """Ship using the check pipeline (--no-verify commit).

    A project without a `ship:` block in fraises.yaml (or no fraises.yaml
    at all) gets a synthetic empty config — the pipeline's check phases
    short-circuit when no checks are configured, but the
    migration-untracked check and commit/push still run.
    """
    import subprocess

    from fraisier.config.schema import ShipConfig as _ShipConfig
    from fraisier.ship.pipeline import ShipPipeline

    config = ship_config or _ShipConfig()
    cwd = Path.cwd()
    pipeline = ShipPipeline(config, cwd, console)

    # Pre-flight: detect untracked migration files that git add --update
    # would silently leave behind (see issue #181)
    migrations_dir = cwd / "db" / "migrations"
    migration_result = pipeline.check_untracked_migrations(migrations_dir)
    if not migration_result.success:
        raise SystemExit(1)

    # Phase 1: auto-fixers (before staging)
    console.print("[bold]Running fix checks...[/bold]")
    fix_result = pipeline.run_fix_phase()
    if not fix_result.success:
        console.print("[red]Fix checks failed, aborting ship.[/red]")
        raise SystemExit(1)

    # Stage all tracked dirty files (bump + fixer output)
    subprocess.run(["git", "add", "--update"], check=True)

    # Phase 2: validators + tests (after staging)
    console.print("[bold]Running validation and tests...[/bold]")
    verify_result = pipeline.run_verify_phase()
    if not verify_result.success:
        console.print("[red]Validation/test checks failed, aborting ship.[/red]")
        raise SystemExit(1)

    # #232: refuse to commit if origin advanced during local CI — the bump
    # we computed before CI would now produce a duplicate-version PR.
    _assert_no_version_race(
        target_version=version,
        expected_base_version=expected_base_version,
        bump_kind=bump_kind,
        pr_base=race_base,
    )

    # Commit with --no-verify (we already ran all checks)
    _commit_release(version)
    _git_push()


def _assert_no_version_race(
    *,
    target_version: str,
    expected_base_version: str,
    bump_kind: str | None,
    pr_base: str | None,
) -> None:
    """Fail loudly when origin's pyproject moved during local CI.

    Two concurrent ``fraisier ship`` invocations can both compute the same
    next version. The second to push produces a duplicate-version PR that
    auto-merge can't land. A short ``git fetch`` here closes the window:
    re-read origin's pyproject and compare against the version we observed
    at start. Mismatch ⇒ abort before commit so recovery is just a rebase.

    *pr_base* is the branch we compare against — the PR target when ``--pr``
    is set (the race is on ``origin/<pr_base>``, not the local feature
    branch), else the current branch.
    """
    import subprocess

    current_branch = _current_branch()
    if current_branch is None:
        # Detached HEAD or non-git tree — can't reason about origin/<branch>.
        return
    race_branch = pr_base or current_branch

    fetch = subprocess.run(
        ["git", "fetch", "--quiet", "origin", race_branch],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if fetch.returncode != 0:
        stderr = (fetch.stderr or "").strip()
        # "couldn't find remote ref" / "no such ref" → branch is missing on
        # origin (first push), there's nothing to race against. Anything
        # else (network, auth, …) is genuinely unknown — warn but proceed
        # so a flaky network can't block ship.
        if "couldn't find remote ref" in stderr or "no such ref" in stderr:
            return
        console.print(
            f"[yellow]Warning:[/yellow] could not fetch origin/{race_branch} "
            f"to verify version-race ({stderr or 'unknown'}); proceeding."
        )
        return

    origin_version = _read_pyproject_version_at_ref(f"origin/{race_branch}")
    if origin_version is None or origin_version == expected_base_version:
        return

    # Roll back the on-disk bump so the operator's working tree is clean
    # and `git pull --ff-only` / `git rebase` will not error on local
    # changes. Best-effort — if the restore fails we still abort.
    restore = subprocess.run(
        ["git", "checkout", "HEAD", "--", "pyproject.toml"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    restored_msg = (
        f"  Your local pyproject.toml has been restored to v{expected_base_version}.\n"
        if restore.returncode == 0
        else "  [yellow]Could not auto-restore pyproject.toml; run "
        "`git checkout HEAD -- pyproject.toml` manually.[/yellow]\n"
    )

    if bump_kind is None:
        # --no-bump re-ship can't recover automatically: origin already has
        # a newer version, so re-shipping at expected_base_version is also
        # a regression. The operator has to decide on a new version.
        next_action = (
            "    # origin/" + race_branch + " already has a newer version than "
            "your tree;\n"
            "    # decide whether to abandon, or re-bump and re-ship.\n"
            "    fraisier ship patch  # or minor/major\n"
        )
    else:
        next_action = f"    fraisier ship {bump_kind}\n"

    console.print(
        f"\n[red]✗ Version race detected.[/red]\n"
        f"  Started at v{expected_base_version}; would push v{target_version}.\n"
        f"  But origin/{race_branch} is now v{origin_version} — "
        f"another ship landed during local CI.\n\n"
        f"{restored_msg}"
        f"  Recover by rebasing onto fresh origin/{race_branch}:\n"
        f"    git checkout {race_branch} && git pull --ff-only\n"
        f"    git checkout {current_branch} && git rebase {race_branch}\n"
        f"{next_action}"
    )
    raise SystemExit(1)


def _current_branch() -> str | None:
    """Return the current branch name, or None on detached HEAD / no repo."""
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _read_pyproject_version_at_ref(ref: str) -> str | None:
    """Read the ``version`` field from pyproject.toml at *ref*, or None."""
    import re
    import subprocess

    result = subprocess.run(
        ["git", "show", f"{ref}:pyproject.toml"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        m = re.match(r'^version\s*=\s*"([^"]+)"', line)
        if m:
            return m.group(1)
    return None


def _resolve_bare_repo_skip() -> Path | None:
    """Return the bare_repo path if it is missing locally, else None.

    Used by dry-run to accurately preview whether a local deploy will be
    skipped in favour of the server-side webhook.  Failures are swallowed
    so dry-run always completes.
    """
    import subprocess as sp

    from fraisier.config import get_config
    from fraisier.deployers.mixins import GitDeployMixin

    from ._helpers import _get_deployer

    try:
        branch = sp.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        config = get_config()
        fraise_config = config.get_fraise_for_branch(branch)
        if not fraise_config:
            return None
        deployer = _get_deployer(fraise_config.get("type"), fraise_config)
        if isinstance(deployer, GitDeployMixin) and not deployer.bare_repo.exists():
            return deployer.bare_repo
    except Exception as exc:
        # Best-effort dry-run helper: any failure (not in a git repo, no
        # config, deployer build error, …) means "can't tell" — degrade
        # silently so dry-run still completes. Log at debug level so the
        # cause is recoverable from verbose logs without polluting normal
        # output. The broad catch is intentional; ``exc`` is bound for
        # observability.
        logger.debug("bare-repo skip probe failed: %s", exc, exc_info=True)
    return None


def _trigger_deploy_for_current_branch(
    wait_deploy: bool = False,
    deploy_timeout: int = 300,
    *,
    no_bump: bool = False,
) -> None:
    """Deploy all fraises mapped to the current git branch."""
    import subprocess as sp

    from fraisier.config import get_config
    from fraisier.locking import deployment_lock

    from ._helpers import _get_deployer

    try:
        branch = sp.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except sp.CalledProcessError:
        console.print("[yellow]Could not detect branch, skipping deploy[/yellow]")
        return

    try:
        config = get_config()
    except FileNotFoundError:
        console.print("[yellow]No fraises.yaml found, skipping deploy[/yellow]")
        return

    fraise_config = config.get_fraise_for_branch(branch)
    if not fraise_config:
        console.print(
            f"[yellow]No fraise mapped to branch '{branch}', skipping deploy[/yellow]"
        )
        return

    fraise_name = fraise_config["fraise_name"]
    environment = fraise_config["environment"]
    fraise_type = fraise_config.get("type")

    deployer = _get_deployer(fraise_type, fraise_config)
    if deployer is None:
        console.print(f"[red]Error:[/red] Unknown fraise type '{fraise_type}'")
        raise SystemExit(1)

    from fraisier.deployers.mixins import GitDeployMixin

    if isinstance(deployer, GitDeployMixin) and not deployer.bare_repo.exists():
        console.print(
            f"[yellow]Skipping local deploy for {fraise_name} ({environment}): "
            f"bare repo {deployer.bare_repo} not found — "
            f"deploy will be triggered via webhook.[/yellow]"
        )
        return

    console.print(f"[green]Deploying {fraise_name} -> {environment}...[/green]")
    try:
        with deployment_lock(fraise_name):
            result = deployer.execute()
    except PermissionError:
        # Lock dir not writable (developer machine) — skip local lock and deploy anyway.
        # The lock only guards against concurrent deploys on the server itself.
        console.print(
            "[yellow]Warning:[/yellow] Cannot acquire deploy lock "
            "(no write access to lock dir) — deploying without lock"
        )
        result = deployer.execute()
    except Exception as e:
        if "already running" in str(e).lower():
            console.print(f"[red]Deploy already running for '{fraise_name}'[/red]")
            raise SystemExit(1) from None
        raise

    if result.success:
        success(f"Deploy successful! {result.old_version} -> {result.new_version}")

        # Poll health endpoint if requested
        if wait_deploy:
            health_config = fraise_config.get("health_check")
            if health_config and "url" in health_config:
                health_url = health_config["url"]
                if no_bump:
                    # Operators running a bring-your-own release-please workflow
                    # can mistake the immediate-success health poll for the
                    # release-PR-triggered deploy they were expecting. Be
                    # explicit: this wait is for the current redeploy only.
                    console.print(
                        f"[cyan]--no-bump: no version change — polling "
                        f"v{result.new_version} to confirm the current "
                        f"redeploy stays healthy. A later release-PR merge "
                        f"(if any) produces a separate deploy.[/cyan]"
                    )
                console.print(f"[cyan]Verifying deployment at {health_url}...[/cyan]")

                from fraisier.ship.health_poll import poll_health_for_version

                poll_result = poll_health_for_version(
                    health_url=health_url,
                    expected_version=result.new_version,
                    timeout=deploy_timeout,
                    interval=10,
                    console_output=True,
                )

                if poll_result.success:
                    console.print(
                        f"[green]✓[/green] Deployment verified! "
                        f"Version {poll_result.final_version} is live "
                        f"({poll_result.elapsed_seconds:.1f}s)"
                    )
                else:
                    final_version = poll_result.final_version or "unknown"
                    console.print(
                        f"[red]✗[/red] Deployment verification failed. "
                        f"Expected {result.new_version}, got {final_version} "
                        f"(timeout after {poll_result.elapsed_seconds:.1f}s)"
                    )
                    raise SystemExit(1)
            else:
                console.print(
                    "[yellow]Warning:[/yellow] No health check URL configured, "
                    "skipping deployment verification"
                )

    else:
        console.print(f"[red]Deploy failed:[/red] {result.error_message}")
        raise SystemExit(1)

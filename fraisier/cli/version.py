"""Version management commands (version show, version bump, ship)."""

from __future__ import annotations

from pathlib import Path

import click

from ._helpers import console
from .main import main


@main.group(name="version", invoke_without_command=True)
@click.pass_context
def version_group(ctx: click.Context) -> None:
    """Version management commands.

    \b
    Without subcommand: show Fraisier package version.
    With subcommand: manage project version.json.

    \b
    Examples:
        fraisier version            # Show package version
        fraisier version show       # Show version.json info
        fraisier version bump patch # Bump patch version
    """
    if ctx.invoked_subcommand is None:
        from fraisier import __version__

        console.print(f"Fraisier v{__version__}")


@version_group.command(name="show")
@click.option(
    "--version-file",
    "-f",
    default="version.json",
    help="Path to version.json",
)
def version_show(version_file: str) -> None:
    """Show project version info from version.json."""
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
    "--version-file",
    "-f",
    default="version.json",
    help="Path to version.json",
)
@click.option("--dry-run", is_flag=True, help="Show what would change")
@click.option("--no-tag", is_flag=True, help="Skip git tag creation")
def version_bump(
    part: str,
    version_file: str,
    dry_run: bool,
    no_tag: bool,  # noqa: ARG001
) -> None:
    """Bump project version (major, minor, or patch).

    \b
    Examples:
        fraisier version bump patch
        fraisier version bump minor --dry-run
        fraisier version bump major --no-tag
    """
    from fraisier.versioning import bump_version, parse_semver, read_version

    path = Path(version_file)
    info = read_version(path)
    if info is None:
        console.print(f"[red]Error:[/red] Version file not found: {version_file}")
        raise SystemExit(1)

    old_version = info.version
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
    "--skip-checks", is_flag=True, help="Skip pipeline checks, just bump+commit+push"
)
@click.option(
    "--version-file",
    type=click.Path(),
    default="version.json",
    help="Path to version.json",
)
@click.option(
    "--pyproject",
    type=click.Path(),
    default="pyproject.toml",
    help="Path to pyproject.toml",
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
    skip_checks: bool,
    version_file: str,
    pyproject: str,
) -> None:
    """Bump version, commit, push, and deploy in one step.

    \b
    Examples:
        fraisier ship patch
        fraisier ship minor --dry-run
        fraisier ship patch --no-deploy
        fraisier ship patch --pr --pr-base dev
        fraisier ship major --skip-checks
        fraisier ship --no-bump
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

    from fraisier.versioning import bump_version, parse_semver

    version_path = Path(version_file)
    pyproject_path = Path(pyproject)
    current = _read_current_version(version_path)

    # Resolve ship config (may be None if no fraises.yaml)
    config = ctx.obj.get("config") if ctx.obj else None
    ship_config = config.ship if config else None
    has_pipeline = bool(ship_config and ship_config.checks and not skip_checks)

    if no_bump:
        if dry_run:
            _ship_dry_run_no_bump(
                current.version,
                version_path,
                ship_config,
                has_pipeline,
                create_pr,
                pr_base,
                no_deploy,
            )
            return

        _ship_commit_push_deploy(
            current,
            ship_config,
            has_pipeline,
            create_pr,
            pr_base,
            no_deploy,
            label=f"v{current.version} (no bump)",
        )
        return

    new = _calc_new_version(current.version, bump_type, parse_semver)

    if dry_run:
        _ship_dry_run(
            current.version,
            new,
            bump_type,
            version_path,
            pyproject_path,
            ship_config,
            has_pipeline,
            create_pr,
            pr_base,
            no_deploy,
        )
        return

    pp = pyproject_path if pyproject_path.exists() else None
    info = bump_version(version_path, bump_type, pyproject_path=pp)
    console.print(f"[green]Version bumped:[/green] {current.version} -> {info.version}")

    _ship_commit_push_deploy(
        info,
        ship_config,
        has_pipeline,
        create_pr,
        pr_base,
        no_deploy,
        label=f"v{info.version}",
    )


def _ship_commit_push_deploy(
    info: object,
    ship_config: object,
    has_pipeline: bool,
    create_pr: bool,
    pr_base: str | None,
    no_deploy: bool,
    *,
    label: str,
) -> None:
    """Run the commit-push-PR-deploy sequence."""
    if has_pipeline:
        _ship_with_pipeline(info, ship_config)
    else:
        _ship_legacy(info)

    if create_pr:
        _ship_create_pr(info.version, pr_base, ship_config)

    console.print(f"[green]Shipped {label}[/green]")

    if not no_deploy:
        _trigger_deploy_for_current_branch()


def _read_current_version(version_path: Path) -> object:
    """Read and validate the current version file."""
    from fraisier.versioning import read_version

    if not version_path.exists():
        console.print(f"[red]Error:[/red] {version_path} not found")
        raise SystemExit(1)
    current = read_version(version_path)
    if current is None:
        console.print(f"[red]Error:[/red] Cannot read {version_path}")
        raise SystemExit(1)
    return current


def _calc_new_version(
    current_version: str,
    bump_type: str,
    parse_semver: object,
) -> str:
    """Calculate the new version string."""
    major, minor, patch_v = parse_semver(current_version)
    if bump_type == "major":
        return f"{major + 1}.0.0"
    if bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch_v + 1}"


def _ship_dry_run(
    current_version: str,
    new: str,
    bump_type: str,
    version_path: Path,
    pyproject_path: Path,
    ship_config: object,
    has_pipeline: bool,
    create_pr: bool,
    pr_base: str | None,
    no_deploy: bool,
) -> None:
    """Print dry-run plan for ship."""
    console.print(f"[cyan]DRY RUN:[/cyan] Would ship v{new}")
    console.print(f"  Bump: {current_version} -> {new} ({bump_type})")
    console.print(f"  Files: {version_path}, {pyproject_path}")
    if has_pipeline:
        console.print("  Pipeline checks:")
        for c in ship_config.checks:
            console.print(f"    [{c.phase}] {c.name}")
    console.print("  Git: add, commit, push")
    if create_pr:
        base = pr_base or (ship_config.pr_base if ship_config else None)
        console.print(f"  PR: create against {base or '<default branch>'}")
    if not no_deploy:
        console.print("  Deploy: trigger for branch-mapped fraises")


def _ship_dry_run_no_bump(
    current_version: str,
    version_path: Path,
    ship_config: object,
    has_pipeline: bool,
    create_pr: bool,
    pr_base: str | None,
    no_deploy: bool,
) -> None:
    """Print dry-run plan for ship --no-bump."""
    console.print(f"[cyan]DRY RUN:[/cyan] Would ship v{current_version} (no bump)")
    console.print(f"  Version: {current_version} (unchanged)")
    console.print(f"  Files: {version_path}")
    if has_pipeline:
        console.print("  Pipeline checks:")
        for c in ship_config.checks:
            console.print(f"    [{c.phase}] {c.name}")
    console.print("  Git: add, commit, push")
    if create_pr:
        base = pr_base or (ship_config.pr_base if ship_config else None)
        console.print(f"  PR: create against {base or '<default branch>'}")
    if not no_deploy:
        console.print("  Deploy: trigger for branch-mapped fraises")


def _ship_create_pr(
    version: str,
    pr_base: str | None,
    ship_config: object,
) -> None:
    """Create a PR after push."""
    base = pr_base or (ship_config.pr_base if ship_config else None)
    if not base:
        console.print(
            "[red]Error:[/red] --pr-base required (or set ship.pr_base in fraises.yaml)"
        )
        raise SystemExit(1)
    from fraisier.ship.pr import create_pr as do_create_pr

    do_create_pr(version, base, console)


def _ship_with_pipeline(
    info: object,
    ship_config: object,
) -> None:
    """Ship using the check pipeline (--no-verify commit)."""
    import subprocess

    from fraisier.ship.pipeline import ShipPipeline

    cwd = Path.cwd()
    pipeline = ShipPipeline(ship_config, cwd, console)

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

    # Commit with --no-verify (we already ran all checks)
    subprocess.run(
        ["git", "commit", "--no-verify", "-m", f"release: v{info.version}"],
        check=True,
    )
    subprocess.run(["git", "push"], check=True)


def _ship_legacy(info: object) -> None:
    """Ship without pipeline (backward compat, uses pre-commit hooks)."""
    import subprocess

    subprocess.run(["git", "add", "--update"], check=True)
    try:
        subprocess.run(
            ["git", "commit", "-m", f"release: v{info.version}"],
            check=True,
        )
    except subprocess.CalledProcessError:
        # Pre-commit hooks may have auto-fixed files (ruff, detect-secrets…)
        # Only retry if the working tree is dirty — otherwise it's a real failure
        diff = subprocess.run(
            ["git", "diff", "--quiet"], capture_output=True, check=False
        )
        if diff.returncode == 0:
            raise
        console.print(
            "[yellow]Pre-commit hooks modified files, staging and retrying...[/yellow]"
        )
        subprocess.run(["git", "add", "--update"], check=True)
        subprocess.run(
            ["git", "commit", "-m", f"release: v{info.version}"],
            check=True,
        )
    subprocess.run(["git", "push"], check=True)


def _trigger_deploy_for_current_branch() -> None:
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

    console.print(f"[green]Deploying {fraise_name} -> {environment}...[/green]")
    try:
        with deployment_lock(fraise_name):
            result = deployer.execute()
    except Exception as e:
        if "already running" in str(e).lower():
            console.print(f"[red]Deploy already running for '{fraise_name}'[/red]")
            raise SystemExit(1) from None
        raise

    if result.success:
        console.print(
            f"[green]Deploy successful![/green] "
            f"{result.old_version} -> {result.new_version}"
        )
    else:
        console.print(f"[red]Deploy failed:[/red] {result.error_message}")
        raise SystemExit(1)

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
@click.argument("bump_type", type=click.Choice(["patch", "minor", "major"]))
@click.option("--dry-run", is_flag=True, help="Show what would happen")
@click.option("--no-deploy", is_flag=True, help="Skip deploy after push")
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
def ship(
    bump_type: str,
    dry_run: bool,
    no_deploy: bool,
    version_file: str,
    pyproject: str,
) -> None:
    """Bump version, commit, push, and deploy in one step.

    \b
    Examples:
        fraisier ship patch
        fraisier ship minor --dry-run
        fraisier ship patch --no-deploy
        fraisier ship major --pyproject path/to/pyproject.toml
    """
    import subprocess

    from fraisier.versioning import bump_version, parse_semver, read_version

    version_path = Path(version_file)
    pyproject_path = Path(pyproject)

    if not version_path.exists():
        console.print(f"[red]Error:[/red] {version_path} not found")
        raise SystemExit(1)

    current = read_version(version_path)
    if current is None:
        console.print(f"[red]Error:[/red] Cannot read {version_path}")
        raise SystemExit(1)

    major, minor, patch_v = parse_semver(current.version)
    if bump_type == "major":
        new = f"{major + 1}.0.0"
    elif bump_type == "minor":
        new = f"{major}.{minor + 1}.0"
    else:
        new = f"{major}.{minor}.{patch_v + 1}"

    if dry_run:
        console.print(f"[cyan]DRY RUN:[/cyan] Would ship v{new}")
        console.print(f"  Bump: {current.version} -> {new} ({bump_type})")
        console.print(f"  Files: {version_path}, {pyproject_path}")
        console.print("  Git: add, commit, push")
        if not no_deploy:
            console.print("  Deploy: trigger for branch-mapped fraises")
        return

    # Bump version atomically
    pp = pyproject_path if pyproject_path.exists() else None
    info = bump_version(version_path, bump_type, pyproject_path=pp)
    console.print(f"[green]Version bumped:[/green] {current.version} -> {info.version}")

    # Git add, commit, push
    files_to_add = [str(version_path)]
    if pp:
        files_to_add.append(str(pp))

    subprocess.run(["git", "add", *files_to_add], check=True)
    subprocess.run(
        ["git", "commit", "-m", f"release: v{info.version}"],
        check=True,
    )
    subprocess.run(["git", "push"], check=True)
    console.print(f"[green]Shipped v{info.version}[/green]")

    # Deploy for branch-mapped fraises
    if not no_deploy:
        _trigger_deploy_for_current_branch()


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

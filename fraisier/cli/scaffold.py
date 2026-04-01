"""Scaffold command for generating infrastructure files."""

from __future__ import annotations

import subprocess
from pathlib import Path

import click

from ._helpers import console, require_config
from .main import main


@main.command(name="scaffold")
@click.option("--dry-run", is_flag=True, help="Show what would be generated")
@click.option(
    "--server",
    "-s",
    default=None,
    help="Only include paths for this server",
)
@click.pass_context
def scaffold(ctx: click.Context, dry_run: bool, server: str | None) -> None:
    """Generate infrastructure files from fraises.yaml.

    Renders systemd units, nginx configs, GitHub Actions workflows,
    sudoers, install scripts, confiture configs, and shell scripts.

    \b
    Examples:
        fraisier scaffold
        fraisier scaffold --dry-run
        fraisier scaffold --server server-1
    """
    from fraisier.scaffold.renderer import ScaffoldRenderer

    config = ctx.obj["config"]
    renderer = ScaffoldRenderer(config, server=server)
    files = renderer.render(dry_run=dry_run)

    if dry_run:
        console.print("[cyan]Would generate the following files:[/cyan]")
        for f in files:
            console.print(f"  {config.scaffold.output_dir}/{f}")
    else:
        console.print(
            f"[green]Generated {len(files)} files "
            f"in {config.scaffold.output_dir}[/green]"
        )
        for f in files:
            console.print(f"  {f}")

        # Provide helpful next steps
        console.print("\n[cyan]Next steps:[/cyan]")
        console.print("  1. Review generated files:")
        console.print(f"     git diff {config.scaffold.output_dir}/")
        console.print("\n  2. Install to system:")
        console.print("     fraisier scaffold-install --dry-run    # Preview")
        console.print("     fraisier scaffold-install --yes        # Install")


def _run_script(cmd: list[str]) -> int:
    """Run a script and return the exit code."""
    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except FileNotFoundError as e:
        console.print(
            "[red]Error:[/red] Could not run script. Please ensure sudo is available.",
            style="bold",
        )
        raise SystemExit(1) from e


def _build_preview_cmd(cmd: list[str]) -> list[str]:
    """Build a preview command by adding --dry-run flag."""
    if "--dry-run" in cmd:
        return cmd
    preview = list(cmd)
    # Insert before other flags
    flag_count = sum(1 for c in preview if c.startswith("--"))
    insert_pos = len(preview) - flag_count
    preview.insert(insert_pos, "--dry-run")
    return preview


@main.command(name="scaffold-install")
@click.option("--dry-run", is_flag=True, help="Preview what would be installed")
@click.option(
    "--validate-only", is_flag=True, help="Check prerequisites only (no install)"
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose output")
@click.pass_context
def scaffold_install(
    ctx: click.Context,
    dry_run: bool,
    validate_only: bool,
    yes: bool,
    verbose: bool,
) -> None:
    """Install generated scaffold files to system locations.

    Runs the generated install.sh script with sudo to install systemd units,
    nginx configs, sudoers rules, wrapper scripts, and system dependencies.

    Must run 'fraisier scaffold' first to generate the files.

    Prerequisites:
    - Must run 'fraisier scaffold' first
    - Must have sudo access (or be running as root)
    - Generated files must be in PROJECT_DIR (usually /opt/<project_name>)

    \b
    Examples:
        fraisier scaffold-install --dry-run     # Preview changes
        fraisier scaffold-install --validate-only # Check prerequisites
        fraisier scaffold-install --yes          # Install without prompt
    """
    config = require_config(ctx)

    # Locate the install.sh script
    output_dir = Path(config.scaffold.output_dir)
    install_script = output_dir / "install.sh"

    if not install_script.exists():
        console.print(
            f"[red]Error:[/red] {install_script} not found.\n"
            "Run 'fraisier scaffold' first to generate it.",
            style="bold",
        )
        raise SystemExit(1)

    if not install_script.is_file():
        console.print(
            f"[red]Error:[/red] {install_script} is not a regular file.",
            style="bold",
        )
        raise SystemExit(1)

    # Make sure install.sh is executable
    install_script.chmod(0o755)

    # Build the command
    cmd: list[str] = ["sudo", str(install_script)]
    if dry_run:
        cmd.append("--dry-run")
    if validate_only:
        cmd.append("--validate-only")
    if verbose:
        cmd.append("--verbose")

    # Show what will happen
    if validate_only:
        console.print("[cyan]Checking prerequisites...[/cyan]\n")
    elif dry_run:
        console.print("[cyan]Preview of what would be installed:[/cyan]\n")
    else:
        console.print("[cyan]Installation plan:[/cyan]\n")

    # If not --yes and not validating/dry-running, show preview first
    if not yes and not validate_only and not dry_run:
        preview_cmd = _build_preview_cmd(cmd)
        _run_script(preview_cmd)
        console.print()
        if not click.confirm("Proceed with installation?"):
            console.print("[yellow]Aborted.[/yellow]")
            return

    # Run the actual command
    returncode = _run_script(cmd)

    if returncode == 0:
        if validate_only:
            console.print("\n[green]✓ All prerequisites met![/green]")
        elif dry_run:
            console.print("\n[green]✓ Preview complete[/green]")
        else:
            console.print(
                "\n[green]✓ Installation complete![/green]\n"
                "[cyan]Next steps:[/cyan]\n"
                "  1. Verify services are running:\n"
                "     systemctl status <service-name>\n"
                "  2. Check deployment:\n"
                "     fraisier deploy <fraise> <environment>"
            )
    else:
        if validate_only or dry_run:
            console.print(
                "\n[yellow]⚠ Preview/validation encountered issues.[/yellow]\n"
                "Review the output above for details."
            )
        else:
            console.print(
                "\n[red]✗ Installation failed[/red]\n"
                "Review the output above for details."
            )
        raise SystemExit(returncode)

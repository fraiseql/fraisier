"""Server setup command for provisioning infrastructure."""

from __future__ import annotations

import click
from rich.table import Table

from ._helpers import console, require_config
from .main import main


@main.command(name="setup")
@click.option("--dry-run", is_flag=True, help="Preview what would be done")
@click.option("--environment", "-e", help="Only setup a single environment")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def setup(
    ctx: click.Context,
    dry_run: bool,
    environment: str | None,
    yes: bool,
) -> None:
    """Provision server infrastructure from fraises.yaml.

    Creates directories, symlinks bare repos, installs systemd services,
    generates webhook env files, installs nginx vhosts, and validates.

    \b
    Examples:
        fraisier setup                    # interactive, shows plan first
        fraisier setup --dry-run          # preview only
        fraisier setup --environment dev  # single environment
        fraisier setup --yes              # skip confirmation
    """
    from fraisier.runners import LocalRunner
    from fraisier.setup import ServerSetup

    config = require_config(ctx)
    runner = LocalRunner()
    server_setup = ServerSetup(config, runner, environment=environment)

    actions = server_setup.plan()

    if not actions:
        console.print("[yellow]Nothing to do.[/yellow]")
        return

    _display_plan(actions)

    if dry_run:
        console.print(f"\n[cyan]{len(actions)} actions would be executed.[/cyan]")
        return

    if not yes and not click.confirm("\nProceed with setup?"):
        console.print("Aborted.")
        return

    results = server_setup.execute()

    succeeded = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)

    if failed:
        console.print(f"\n[red]{failed} actions failed[/red], {succeeded} succeeded")
        for action, ok in results:
            if not ok:
                console.print(f"  [red]FAIL[/red] {action.description}")
        raise SystemExit(1)
    else:
        console.print(
            f"\n[green]All {succeeded} actions completed successfully.[/green]"
        )


def _display_plan(actions: list) -> None:
    """Render the plan as a Rich table."""
    table = Table(title="Setup Plan", show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Category", style="cyan", width=12)
    table.add_column("Action")

    for i, action in enumerate(actions, 1):
        table.add_row(str(i), action.category, action.description)

    console.print(table)

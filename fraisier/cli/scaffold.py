"""Scaffold command for generating infrastructure files."""

from __future__ import annotations

import click

from ._helpers import console
from .main import main


@main.command(name="scaffold")
@click.option("--dry-run", is_flag=True, help="Show what would be generated")
@click.pass_context
def scaffold(ctx: click.Context, dry_run: bool) -> None:
    """Generate infrastructure files from fraises.yaml.

    Renders systemd units, nginx configs, GitHub Actions workflows,
    sudoers, install scripts, confiture configs, and shell scripts.

    \b
    Examples:
        fraisier scaffold
        fraisier scaffold --dry-run
    """
    from fraisier.scaffold.renderer import ScaffoldRenderer

    config = ctx.obj["config"]
    renderer = ScaffoldRenderer(config)
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

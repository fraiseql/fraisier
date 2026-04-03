"""Logs command for tailing systemd journal."""

from __future__ import annotations

import os

import click

from fraisier.cli._helpers import console, require_config
from fraisier.cli.main import main


def _resolve_deploy_unit_pattern(config, fraise: str, environment: str) -> str:
    """Resolve the systemd unit pattern for deploy services.

    Args:
        config: Fraisier config
        fraise: Fraise name
        environment: Environment name

    Returns:
        Unit pattern like "fraisier-{project}-{fraise}-{env}-deploy@*.service"
    """
    project = config.project_name
    prefix = f"fraisier-{project}-{fraise}-{environment}-deploy"
    return f"{prefix}@*.service"


@main.command()
@click.argument("fraise")
@click.argument("environment")
@click.option("--no-follow", is_flag=True, help="Don't follow, just dump")
@click.option("--lines", "-n", default=50, help="Number of lines to show")
@click.option("--since", default=None, help="Show logs since (e.g. '10 minutes ago')")
@click.pass_context
def logs(
    ctx: click.Context,
    fraise: str,
    environment: str,
    no_follow: bool,
    lines: int,
    since: str | None,
) -> None:
    """Tail systemd journal logs for the deploy daemon.

    Shows logs from fraisier deploy services for the specified fraise and environment.
    By default follows logs in real-time. Use --no-follow to dump and exit.

    \b
    Examples:
        fraisier logs api production                    # follow logs
        fraisier logs api production --no-follow       # dump last 50 lines
        fraisier logs api production --lines 100       # last 100 lines
        fraisier logs api production --since "1 hour ago"  # logs from last hour
    """
    config = require_config(ctx)

    # Validate fraise/environment exists
    fraise_config = config.get_fraise_environment(fraise, environment)
    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise click.Exit(1)

    # Build unit pattern
    unit_pattern = _resolve_deploy_unit_pattern(config, fraise, environment)

    # Build journalctl command
    cmd = ["journalctl", "-u", unit_pattern, "-n", str(lines)]

    if not no_follow:
        cmd.append("-f")

    if since:
        cmd.extend(["--since", since])

    # Replace this process with journalctl
    os.execvp("journalctl", cmd)

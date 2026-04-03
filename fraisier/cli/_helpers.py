"""Shared utilities for CLI commands."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import click
from rich.console import Console

if TYPE_CHECKING:
    from fraisier.config import FraisierConfig

console = Console()


def parse_since(value: str) -> str:
    """Parse relative time or date string into ISO datetime.

    Args:
        value: Time string like "7d", "24h", "1h", or ISO date "2026-04-01"

    Returns:
        ISO datetime string

    Raises:
        ValueError: If the format is invalid
    """
    if not value:
        return ""

    # Check for relative time patterns
    relative_pattern = re.match(r"^(\d+)([dh])$", value)
    if relative_pattern:
        amount, unit = relative_pattern.groups()
        amount = int(amount)
        if unit == "d":
            delta = timedelta(days=amount)
        elif unit == "h":
            delta = timedelta(hours=amount)
        else:
            raise ValueError(f"Invalid time unit: {unit}")

        target_time = datetime.now() - delta
        return target_time.isoformat()

    # Check if it's already an ISO date (YYYY-MM-DD)
    try:
        parsed = datetime.fromisoformat(value)
        # If it's just a date, convert to start of that day
        if "T" not in value:
            parsed = datetime.combine(parsed.date(), datetime.min.time())
        return parsed.isoformat()
    except ValueError as err:
        raise ValueError(
            f"Invalid date/time format: {value}. Use '7d', '24h', or 'YYYY-MM-DD'"
        ) from err


def require_config(ctx: click.Context) -> FraisierConfig:
    """Get config from context, aborting with a clear error if missing."""
    config = ctx.obj.get("config")
    if config is None:
        raise click.UsageError(
            "No fraises.yaml config found. "
            "Run 'fraisier init' to create one or use --config to specify a path."
        )
    return config


def _print_dry_run(
    config: FraisierConfig,
    fraise: str,
    environment: str,
    fraise_config: dict,
) -> None:
    """Print a detailed dry-run deployment plan."""
    from rich.panel import Panel
    from rich.table import Table

    fraise_type = fraise_config.get("type", "unknown")
    strategy = (
        fraise_config.get("database", {}).get("strategy")
        or config.deployment.get_strategy(environment)
        or "basic"
    )
    db = fraise_config.get("database")
    hc = fraise_config.get("health_check")
    service = fraise_config.get("systemd_service")
    app_path = fraise_config.get("app_path", "")

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Step", style="bold cyan", min_width=16)
    table.add_column("Details")

    table.add_row("Target", f"{fraise} -> {environment}")
    table.add_row("Type", fraise_type)
    table.add_row("Strategy", strategy)
    if app_path:
        table.add_row("App path", app_path)

    # Database / backup / migration
    if db:
        db_name = db.get("name", "unknown")
        db_strategy = db.get("strategy", "none")
        if db.get("backup_before_deploy"):
            table.add_row("Backup", f"confiture preflight on {db_name}")
        table.add_row(
            "Migration",
            f"confiture migrate up on {db_name} (strategy: {db_strategy})",
        )
    else:
        table.add_row("Database", "none (no database configured)")

    # Service restart
    if service:
        table.add_row("Restart", service)

    # Health check
    if hc:
        url = hc.get("url", "")
        timeout = hc.get("timeout", 30)
        table.add_row("Health check", f"{url} (timeout: {timeout}s)")
    else:
        table.add_row("Health check", "none (skipped)")

    console.print(Panel(table, title="[cyan]DRY RUN[/cyan]", expand=False))


def _get_deployer(fraise_type: str | None, fraise_config: dict, job: str | None = None):
    """Get appropriate deployer for fraise type.

    When the fraise_config contains an ``ssh`` key, the deployer is
    configured with an ``SSHRunner`` so that commands execute on the
    remote host.  Otherwise a local ``LocalRunner`` is used.
    """
    from fraisier.runners import runner_from_config

    runner = runner_from_config(fraise_config.get("ssh"))

    if fraise_type == "api":
        from fraisier.deployers.api import APIDeployer

        return APIDeployer(fraise_config, runner=runner)

    elif fraise_type == "etl":
        from fraisier.deployers.etl import ETLDeployer

        return ETLDeployer(fraise_config, runner=runner)

    elif fraise_type == "docker_compose":
        from fraisier.deployers.docker_compose import DockerComposeDeployer

        return DockerComposeDeployer(fraise_config, runner=runner)

    elif fraise_type in ("scheduled", "backup"):
        from fraisier.deployers.scheduled import ScheduledDeployer

        # Handle nested jobs
        if job and "jobs" in fraise_config:
            job_config = fraise_config["jobs"].get(job)
            if job_config:
                return ScheduledDeployer(
                    {
                        **fraise_config,
                        **job_config,
                        "job_name": job,
                    },
                    runner=runner,
                )
        return ScheduledDeployer(fraise_config, runner=runner)

    return None

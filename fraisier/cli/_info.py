"""Informational CLI commands: init, list, status."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.table import Table
from rich.tree import Tree

from fraisier.status import elapsed_seconds, read_status

from ._helpers import _get_deployer, console
from .main import main

if TYPE_CHECKING:
    from fraisier.config.loader import FraisierConfig
    from fraisier.deployers.base import BaseDeployer


@main.command()
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=".",
    help="Directory to create fraises.yaml in",
)
@click.option(
    "--template",
    "-t",
    type=click.Choice(["generic", "django", "rails", "node"]),
    default="generic",
    help="Project template to use",
)
@click.option("--force", is_flag=True, help="Overwrite existing fraises.yaml")
def init(output: str, template: str, force: bool) -> None:
    """Scaffold a new fraises.yaml configuration file.

    \b
    Examples:
        fraisier init
        fraisier init --template django
        fraisier init --template rails -o /opt/myapp
    """
    from fraisier.init_templates import TEMPLATES

    output_path = Path(output)
    config_file = output_path / "fraises.yaml"

    if config_file.exists() and not force:
        console.print(
            f"[red]Error:[/red] {config_file} already exists. Use --force to overwrite."
        )
        raise SystemExit(1)

    output_path.mkdir(parents=True, exist_ok=True)
    template_fn = TEMPLATES[template]
    config_file.write_text(template_fn())
    console.print(f"[green]Created[/green] {config_file} (template: {template})")


@main.command(name="list")
@click.option("--flat", is_flag=True, help="Show flat list instead of grouped")
@click.pass_context
def list_(ctx: click.Context, flat: bool) -> None:
    """List all registered fraises and their environments.

    \b
    Examples:
        fraisier list
        fraisier list --flat
    """
    config = ctx.obj["config"]

    if flat:
        # Flat list of all deployable targets
        deployments = config.list_all_deployments()

        table = Table(title="All Deployable Targets")
        table.add_column("Fraise", style="cyan")
        table.add_column("Environment", style="magenta")
        table.add_column("Job", style="yellow")
        table.add_column("Type", style="green")
        table.add_column("Name")

        for d in deployments:
            table.add_row(
                d["fraise"],
                d["environment"],
                d["job"] or "-",
                d["type"],
                d["name"],
            )

        console.print(table)
    else:
        # Grouped tree view
        tree = Tree("[bold]Fraises[/bold]")

        for fraise in config.list_fraises_detailed():
            fraise_branch = tree.add(
                f"[cyan]{fraise['name']}[/cyan] "
                f"[dim]({fraise['type']})[/dim] - {fraise['description']}"
            )

            for env in fraise["environments"]:
                env_config = config.get_fraise_environment(fraise["name"], env)
                name = env_config.get("name", env) if env_config else env

                # Check for nested jobs
                if env_config and "jobs" in env_config:
                    env_branch = fraise_branch.add(f"[magenta]{env}[/magenta]")
                    for job_name, job_config in env_config["jobs"].items():
                        job_desc = job_config.get("description", "")
                        env_branch.add(f"[yellow]{job_name}[/yellow] - {job_desc}")
                else:
                    fraise_branch.add(f"[magenta]{env}[/magenta] -> {name}")

        console.print(tree)


@main.command()
@click.argument("fraise", required=False, default=None)
@click.argument("environment", required=False, default=None)
@click.option(
    "--server",
    default=None,
    help="Filter by server hostname (default: current hostname)",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show fraises for all servers, not just the current one",
)
@click.pass_context
def status(
    ctx: click.Context,
    fraise: str | None,
    environment: str | None,
    server: str | None,
    show_all: bool,
) -> None:
    """Check status of fraise(s).

    Shows deployment status and health.

    In global view, output is filtered to environments whose server field
    matches the current hostname. Use --all to show all servers.

    \b
    Examples:
        fraisier status                        # Global view filtered to current server
        fraisier status --all                  # Show fraises for all servers
        fraisier status --server printoptim.io # Show fraises for a specific server
        fraisier status my_api production      # Single fraise view
    """
    config = ctx.obj["config"]

    # Validation: if fraise given but environment not, or vice versa
    if (fraise is None) != (environment is None):
        console.print(
            "[red]Error:[/red] Both fraise and environment required together, "
            "or omit both for global view"
        )
        raise SystemExit(1)

    # Global view: show all fraises/environments in a table
    if fraise is None:
        if show_all:
            server_filter = None
        elif server is not None:
            server_filter = server
        else:
            server_filter = socket.gethostname()
        _show_global_status(config, server_filter=server_filter)
        return

    # Single fraise view: existing behavior
    assert environment is not None  # guaranteed by the check above
    _show_single_status(config, fraise, environment)


def _compute_deployment_state(
    fraise_name: str, current: str | None, latest: str | None
) -> str:
    """Compute deployment state string, checking status file first."""
    # Check status file for active deployment states
    status = read_status(fraise_name)
    if status:
        if status.state == "deploying":
            elapsed = elapsed_seconds(status)
            if elapsed is not None:
                return f"[blue]deploying ({int(elapsed)}s)[/blue]"
            return "[blue]deploying[/blue]"
        elif status.state == "pending":
            return "[yellow]pending[/yellow]"
        elif status.state == "failed":
            return "[red]failed[/red]"
        elif status.state == "rollback_failed":
            # The schema is dirty and the service must not be restarted. This
            # is the loudest state the CLI has (#293).
            return "[bold red]ROLLBACK FAILED — schema dirty[/bold red]"
        elif status.state == "rolled_back":
            # Written since before #293 but never matched here, so a rolled-back
            # deploy fell through to version comparison and showed as a green
            # "deployed ✓" whenever the reverted tree matched the latest tag.
            return "[yellow]rolled back[/yellow]"
        elif status.state in ("idle", "success"):
            # If status file shows idle/success, check if versions match
            if current == latest and current is not None:
                return "[green]idle ✓[/green]"
            elif current is not None and latest is not None:
                return "[yellow]out-of-date[/yellow]"

    # Fall back to version comparison when no status file or unknown state
    if current == latest and current is not None:
        return "[green]deployed ✓[/green]"
    if current is None or latest is None:
        return "[dim]unknown[/dim]"
    return "[yellow]out-of-date[/yellow]"


def _compute_health_string(fraise_config: dict, deployer: BaseDeployer) -> str:
    """Compute health status string based on config and deployer health check."""
    health_check_cfg = fraise_config.get("health_check", {})
    has_health = health_check_cfg.get("url") is not None
    has_timer = fraise_config.get("systemd_timer") is not None

    if not has_health and not has_timer:
        return "[dim]not configured[/dim]"

    health_ok = deployer.health_check()
    return "[green]healthy ✓[/green]" if health_ok else "[red]unhealthy[/red]"


def _show_global_status(
    config: FraisierConfig | None, server_filter: str | None = None
) -> None:
    """Display deployment status table for all fraises/environments.

    When *server_filter* is set, only environments whose ``server`` field
    matches that hostname are shown.
    """
    from fraisier.database import get_db

    if config is None:
        console.print("[yellow]No configuration loaded[/yellow]")
        return

    title = "[bold]Deployment Status[/bold]"
    if server_filter is not None:
        title = f"[bold]Deployment Status[/bold] — {server_filter}"

    table = Table(title=title, expand=True)
    table.add_column("Fraise", style="cyan", min_width=15)
    table.add_column("Environment", style="magenta", min_width=15)
    table.add_column("Deployed", style="dim", min_width=10)
    table.add_column("Deployed At", style="dim", min_width=12)
    table.add_column("Latest", style="dim", min_width=10)
    table.add_column("Status", style="yellow", min_width=15)
    table.add_column("Health", style="yellow", min_width=15)

    # Build deployed version lookup from DB
    db = get_db()
    fraise_states = {
        (s["fraise_name"], s["environment_name"]): s for s in db.get_all_fraise_states()
    }

    deployments = config.list_all_deployments()

    # Filter by server when requested
    if server_filter is not None:
        allowed_envs = set(config.get_environments_for_server(server_filter))
        deployments = [d for d in deployments if d["environment"] in allowed_envs]

    if not deployments:
        console.print("[yellow]No fraises configured[/yellow]")
        return

    for d in deployments:
        fraise_name = d["fraise"]
        environment_name = d["environment"]

        try:
            fraise_config = config.get_fraise_environment(fraise_name, environment_name)
            if not fraise_config:
                table.add_row(
                    fraise_name,
                    environment_name,
                    "-",
                    "-",
                    "-",
                    "[red]error[/red]",
                    "-",
                )
                continue

            # Get deployer to check versions and health
            deployer = _get_deployer(
                fraise_config.get("type"), fraise_config, d.get("job")
            )
            if not deployer:
                table.add_row(
                    fraise_name,
                    environment_name,
                    "-",
                    "-",
                    "-",
                    "[red]unsupported type[/red]",
                    "-",
                )
                continue

            current = deployer.get_current_version()
            latest = deployer.get_latest_version()
            status_str = _compute_deployment_state(fraise_name, current, latest)
            health_str = _compute_health_string(fraise_config, deployer)

            # Get deployed timestamp from DB
            state = fraise_states.get((fraise_name, environment_name))
            deployed_at = state["last_deployed_at"][:10] if state else "-"

            table.add_row(
                fraise_name,
                environment_name,
                current or "-",
                deployed_at,
                latest or "-",
                status_str,
                health_str,
            )

        except (
            OSError,
            ValueError,
            KeyError,
            AttributeError,
            TypeError,
            RuntimeError,
        ) as e:
            # Best-effort row: skip a single broken fraise without aborting
            # the table. Expected modes are I/O / network failures (OSError
            # covers ConnectionError + TimeoutError), malformed config
            # (KeyError, AttributeError, TypeError), parsing errors
            # (ValueError, including JSONDecodeError), and runtime errors
            # raised by deployer probes. Anything else (e.g. a real bug
            # in a deployer) propagates so it isn't silently masked.
            console.print(
                f"[yellow]Warning:[/yellow] Error checking "
                f"{fraise_name}/{environment_name}: {e}"
            )
            table.add_row(
                fraise_name,
                environment_name,
                "-",
                "-",
                "-",
                "[red]error[/red]",
                "-",
            )

    console.print(table)


def _show_single_status(config: FraisierConfig, fraise: str, environment: str) -> None:
    """Display deployment status for a single fraise/environment."""
    fraise_config = config.get_fraise_environment(fraise, environment)

    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    console.print(f"[bold]Fraise:[/bold] {fraise}")
    console.print(f"[bold]Environment:[/bold] {environment}")
    console.print(f"[bold]Type:[/bold] {fraise_config.get('type')}")
    console.print(f"[bold]Name:[/bold] {fraise_config.get('name')}")

    if fraise_config.get("systemd_service"):
        console.print(f"[bold]Systemd:[/bold] {fraise_config.get('systemd_service')}")

    # Get deployer and check actual status
    deployer = _get_deployer(fraise_config.get("type"), fraise_config)

    if deployer:
        try:
            current_version = deployer.get_current_version()
            latest_version = deployer.get_latest_version()
            health_ok = deployer.health_check()

            console.print()
            console.print(
                f"[bold]Current Version:[/bold] {current_version or 'unknown'}"
            )
            console.print(f"[bold]Latest Version:[/bold] {latest_version or 'unknown'}")

            health_status = (
                "[green]healthy[/green]" if health_ok else "[red]unhealthy[/red]"
            )
            console.print(f"[bold]Health Check:[/bold] {health_status}")

            # Show deployment state
            deployment_status = _compute_deployment_state(
                fraise, current_version, latest_version
            )
            console.print(f"[bold]Status:[/bold] {deployment_status}")

            # Show recent deployments
            from fraisier.database import get_db

            db = get_db()
            recent = db.get_recent_deployments(
                limit=3, fraise=fraise, environment=environment
            )

            if recent:
                console.print("\n[bold]Recent Deployments:[/bold]")
                for d in recent[:1]:  # Show most recent
                    status_color = "green" if d["status"] == "success" else "red"
                    console.print(
                        f"  [{status_color}]{d['status']}[/{status_color}] "
                        f"({d['old_version']} \u2192 {d['new_version']}) "
                        f"at {d['started_at'][:10]}"
                    )

        except (
            OSError,
            ValueError,
            KeyError,
            AttributeError,
            TypeError,
            RuntimeError,
        ) as e:
            # Single-fraise status probe: same expected modes as the
            # global status loop above (I/O, malformed config, parsing,
            # runtime probe errors). Unexpected exceptions propagate so
            # genuine bugs surface instead of being printed as a
            # one-line warning.
            console.print(f"\n[red]Error checking status:[/red] {e}")

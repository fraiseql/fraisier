"""Operational commands (status-all, history, stats, deploy-status, etc.)."""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.table import Table

from ._helpers import console
from .main import main


@main.command(name="status-all")
@click.option("--environment", "-e", help="Filter by environment")
@click.option("--type", "-t", "fraise_type", help="Filter by fraise type")
@click.pass_context
def status_all(
    ctx: click.Context, environment: str | None, fraise_type: str | None
) -> None:
    """Check status of all fraises."""
    from fraisier.database import get_db

    config = ctx.obj["config"]
    db = get_db()

    # Get fraise states from database
    all_states = db.get_all_fraise_states()

    if environment:
        all_states = [s for s in all_states if s["environment_name"] == environment]
    if fraise_type:
        first_state = all_states[0] if all_states else None
        fraise_config = (
            config.get_fraise(first_state["fraise_name"]) if first_state else None
        )
        if fraise_config:
            all_states = [
                s for s in all_states if fraise_config.get("type") == fraise_type
            ]

    if not all_states:
        console.print("[yellow]No fraises found matching filters[/yellow]")
        return

    table = Table(title="Fraise Status")
    table.add_column("Fraise", style="cyan")
    table.add_column("Environment", style="magenta")
    table.add_column("Type", style="green")
    table.add_column("Current", style="yellow")
    table.add_column("Status")
    table.add_column("Last Deploy", style="dim")

    for state in all_states:
        fraise_name = state["fraise_name"]
        env_name = state["environment_name"]
        fraise_cfg = config.get_fraise(fraise_name)
        fraise_type_str = fraise_cfg.get("type", "unknown") if fraise_cfg else "unknown"
        current_version = state.get("current_version") or "unknown"

        # Format status with color
        db_status = state.get("status", "unknown")
        if db_status == "healthy":
            status_str = "[green]healthy[/green]"
        elif db_status == "degraded":
            status_str = "[yellow]degraded[/yellow]"
        elif db_status == "down":
            status_str = "[red]down[/red]"
        else:
            status_str = "[dim]unknown[/dim]"

        last_deploy = (
            state.get("last_deployed_at", "")[:10]
            if state.get("last_deployed_at")
            else "-"
        )

        table.add_row(
            fraise_name,
            env_name,
            fraise_type_str,
            current_version,
            status_str,
            last_deploy,
        )

    console.print(table)


@main.command()
@click.option("--fraise", "-f", help="Filter by fraise")
@click.option("--environment", "-e", help="Filter by environment")
@click.option("--limit", "-n", default=20, help="Number of records to show")
@click.pass_context
def history(
    _ctx: click.Context, fraise: str | None, environment: str | None, limit: int
) -> None:
    """Show deployment history."""
    from fraisier.database import get_db

    db = get_db()
    deployments = db.get_recent_deployments(
        limit=limit, fraise=fraise, environment=environment
    )

    if not deployments:
        console.print("[yellow]No deployment history found[/yellow]")
        return

    table = Table(title="Deployment History")
    table.add_column("ID", style="dim")
    table.add_column("Fraise", style="cyan")
    table.add_column("Env", style="magenta")
    table.add_column("Version", style="green")
    table.add_column("Status")
    table.add_column("Duration", style="yellow")
    table.add_column("Started", style="dim")

    for d in deployments:
        # Format status with color
        status = d["status"]
        if status == "success":
            status_str = "[green]success[/green]"
        elif status == "failed":
            status_str = "[red]failed[/red]"
        elif status == "rolled_back":
            status_str = "[yellow]rolled back[/yellow]"
        elif status == "in_progress":
            status_str = "[blue]in progress[/blue]"
        else:
            status_str = status

        # Format duration
        duration = d.get("duration_seconds")
        duration_str = f"{duration:.1f}s" if duration else "-"

        # Format version change
        old_v = d.get("old_version") or "?"
        new_v = d.get("new_version") or "?"
        version_str = f"{old_v} -> {new_v}"

        # Format timestamp (just time if today)
        started = d.get("started_at", "")[:16].replace("T", " ")

        table.add_row(
            str(d["id"]),
            d["fraise"],
            d["environment"],
            version_str,
            status_str,
            duration_str,
            started,
        )

    console.print(table)


@main.command()
@click.option("--fraise", "-f", help="Filter by fraise")
@click.option("--days", "-d", default=30, help="Number of days to analyze")
@click.pass_context
def stats(_ctx: click.Context, fraise: str | None, days: int) -> None:
    """Show deployment statistics."""
    from fraisier.database import get_db

    db = get_db()
    s = db.get_deployment_stats(fraise=fraise, days=days)

    if not s.get("total"):
        console.print(f"[yellow]No deployments in the last {days} days[/yellow]")
        return

    title = f"Deployment Stats (last {days} days)"
    if fraise:
        title += f" - {fraise}"

    console.print(f"\n[bold]{title}[/bold]\n")

    total = s.get("total", 0)
    successful = s.get("successful", 0)
    failed = s.get("failed", 0)
    rolled_back = s.get("rolled_back", 0)
    avg_duration = s.get("avg_duration")

    success_rate = (successful / total * 100) if total > 0 else 0

    console.print(f"  Total deployments:  {total}")
    console.print(
        f"  [green]Successful:[/green]        {successful} ({success_rate:.1f}%)"
    )
    console.print(f"  [red]Failed:[/red]            {failed}")
    console.print(f"  [yellow]Rolled back:[/yellow]       {rolled_back}")

    if avg_duration:
        console.print(f"  Avg duration:       {avg_duration:.1f}s")

    console.print()


@main.command()
@click.option("--limit", "-n", default=10, help="Number of events to show")
def webhooks(limit: int) -> None:
    """Show recent webhook events."""
    from fraisier.database import get_db

    db = get_db()
    events = db.get_recent_webhooks(limit=limit)

    if not events:
        console.print("[yellow]No webhook events recorded[/yellow]")
        return

    table = Table(title="Recent Webhook Events")
    table.add_column("ID", style="dim")
    table.add_column("Time", style="dim")
    table.add_column("Event", style="cyan")
    table.add_column("Branch", style="magenta")
    table.add_column("Commit", style="yellow")
    table.add_column("Processed")
    table.add_column("Deploy ID")

    for e in events:
        processed = "[green]yes[/green]" if e["processed"] else "[dim]-[/dim]"
        commit = (e.get("commit_sha") or "")[:8]
        time_str = e.get("received_at", "")[:16].replace("T", " ")

        table.add_row(
            str(e["id"]),
            time_str,
            e["event_type"],
            e.get("branch") or "-",
            commit or "-",
            processed,
            str(e.get("deployment_id") or "-"),
        )

    console.print(table)


@main.command(name="deploy-status")
@click.option(
    "--status-file",
    "-s",
    default="deployment_status.json",
    help="Path to deployment status JSON file",
)
@click.pass_context
def deploy_status(_ctx: click.Context, status_file: str) -> None:
    """Show the last deployment status from status file.

    Reads the deployment_status.json written by the deploy runner
    and displays its contents.

    \b
    Examples:
        fraisier deploy-status
        fraisier deploy-status --status-file /run/myapp/status.json
    """
    status_path = Path(status_file)
    if not status_path.exists():
        console.print("[yellow]No status file found[/yellow]")
        console.print(f"[dim]Looked for: {status_path}[/dim]")
        return

    try:
        data = json.loads(status_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        console.print(f"[red]Error reading status file:[/red] {e}")
        raise SystemExit(1) from e

    # Display status
    status_val = data.get("status", "unknown")
    if status_val == "success":
        status_str = "[green]success[/green]"
    elif status_val == "failed":
        status_str = "[red]failed[/red]"
    else:
        status_str = f"[yellow]{status_val}[/yellow]"

    console.print(f"[bold]Fraise:[/bold]      {data.get('fraise', '?')}")
    console.print(f"[bold]Environment:[/bold] {data.get('environment', '?')}")
    console.print(f"[bold]Status:[/bold]      {status_str}")
    duration = data.get("duration_seconds")
    if duration is not None:
        console.print(f"[bold]Duration:[/bold]    {duration:.1f}s")
    ts = data.get("timestamp", "")
    if ts:
        console.print(f"[bold]Timestamp:[/bold]   {ts}")
    if data.get("error"):
        console.print(f"[bold]Error:[/bold]       {data['error']}")


@main.command(name="metrics")
@click.option("--port", "-p", default=8001, type=int, help="Port for metrics server")
@click.option("--address", "-a", default="localhost", help="Address to bind to")
def metrics_endpoint(port: int, address: str) -> None:
    """Start Prometheus metrics exporter endpoint.

    Exports deployment metrics at http://ADDRESS:PORT/metrics

    \b
    Examples:
        fraisier metrics                    # Start on localhost:8001
        fraisier metrics --port 8080        # Use port 8080
        fraisier metrics --address 0.0.0.0  # Listen on all interfaces
    """
    try:
        from prometheus_client import start_http_server
    except ImportError as e:
        console.print(
            "[red]Error:[/red] prometheus_client not installed\n"
            "[yellow]Install with:[/yellow] pip install prometheus-client"
        )
        raise SystemExit(1) from e

    try:
        # Start metrics server
        start_http_server(port, addr=address)
        console.print(
            f"[green]\u2713 Prometheus metrics server started[/green]\n"
            f"Metrics available at: [cyan]http://{address}:{port}/metrics[/cyan]\n"
            f"[dim]Press Ctrl+C to stop[/dim]"
        )

        # Keep server running
        import time

        while True:
            time.sleep(1)

    except OSError as e:
        console.print(f"[red]Error:[/red] Failed to start metrics server: {e}")
        raise SystemExit(1) from e
    except KeyboardInterrupt:
        console.print("\n[yellow]Metrics server stopped[/yellow]")
        raise SystemExit(0) from None

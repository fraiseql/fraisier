"""Health and validation commands."""

from __future__ import annotations

import click
from rich.table import Table

from ._helpers import console
from .main import main


@main.command(name="health")
@click.option("--env", "-e", help="Filter by environment")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--wait",
    "-w",
    is_flag=True,
    help="Wait for all services to be healthy",
)
@click.pass_context
def health(
    ctx: click.Context,
    env: str | None,
    as_json: bool,
    wait: bool,  # noqa: ARG001
) -> None:
    """Check health of all services.

    \b
    Examples:
        fraisier health
        fraisier health --json
        fraisier health --env production
    """
    from fraisier.health_check import AggregateHealthChecker

    config = ctx.obj["config"]
    health_config = config.health

    # Build service map: name -> base URL from fraise port configs
    services: dict[str, str] = {}
    for fraise_name in config.list_fraises():
        fraise = config.get_fraise(fraise_name)
        if not fraise:
            continue
        for env_name, env_config in fraise.get("environments", {}).items():
            if env and env_name != env:
                continue
            port = env_config.get("port")
            health_url = env_config.get("health_check", {})
            if isinstance(health_url, dict):
                health_url = health_url.get("url")
            if health_url:
                services[fraise_name] = health_url.rsplit("/", 1)[0]
            elif port:
                services[fraise_name] = f"http://localhost:{port}"

    checker = AggregateHealthChecker(
        services=services,
        health_config=health_config,
    )
    result = checker.check_all()

    if as_json:
        import json

        data = result.to_dict(response_config=health_config.response)
        click.echo(json.dumps(data, indent=2))
        return

    # Rich table output
    status_color = {
        "healthy": "green",
        "degraded": "yellow",
        "unhealthy": "red",
    }
    color = status_color.get(result.status, "white")
    console.print(
        f"\n[bold]Aggregate Status:[/bold] [{color}]{result.status}[/{color}]"
    )

    if result.services:
        table = Table(title="Service Health")
        table.add_column("Service", style="cyan")
        table.add_column("URL", style="dim")
        table.add_column("Status")
        table.add_column("Response", style="yellow")

        for svc_name, svc in result.services.items():
            svc_color = "green" if svc.status == "healthy" else "red"
            table.add_row(
                svc_name,
                svc.url,
                f"[{svc_color}]{svc.status}[/{svc_color}]",
                f"{svc.response_time_ms:.1f}ms",
            )

        console.print(table)

    console.print(f"[dim]Total: {result.response_time_ms:.1f}ms[/dim]\n")


@main.command(name="validate")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def validate(ctx: click.Context, as_json: bool) -> None:
    """Run pre-deploy validation checks.

    Checks: config validity, deploy user, fraise environments.

    \b
    Examples:
        fraisier validate
        fraisier validate --json
    """
    import json

    from fraisier.validation import ValidationRunner

    config = ctx.obj["config"]
    runner = ValidationRunner(config)
    results = runner.run_all()

    all_passed = all(r.passed for r in results)

    if as_json:
        data = {
            "passed": all_passed,
            "checks": [r.to_dict() for r in results],
        }
        click.echo(json.dumps(data, indent=2))
    else:
        errors = [r for r in results if not r.passed and r.severity == "error"]
        warnings = [r for r in results if not r.passed and r.severity == "warning"]
        passed = [r for r in results if r.passed]

        if errors:
            console.print("[bold red]Errors:[/bold red]")
            for r in errors:
                console.print(f"  [red]ERROR[/red]  {r.name} \u2014 {r.message}")

        if warnings:
            console.print("[bold yellow]Warnings:[/bold yellow]")
            for r in warnings:
                console.print(f"  [yellow]WARN[/yellow]   {r.name} \u2014 {r.message}")

        if passed:
            console.print(f"[green]{len(passed)} checks passed[/green]")

        if all_passed:
            console.print("\n[green]All checks passed[/green]")
        else:
            console.print(
                f"\n[red]{len(errors)} error(s), {len(warnings)} warning(s)[/red]"
            )

    raise SystemExit(0 if all_passed else 1)

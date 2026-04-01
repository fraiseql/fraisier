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


@main.command(name="validate-deployment")
@click.argument("fraise")
@click.argument("environment")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def validate_deployment(
    ctx: click.Context, fraise: str, environment: str, as_json: bool
) -> None:
    """Validate a fraise/environment is ready for deployment.

    Checks: git repo, app_path, database config, systemd service, wrappers,
    sudoers, health endpoint, and install dependencies.

    \b
    Examples:
        fraisier validate-deployment my_api production
        fraisier validate-deployment my_api staging --json
    """
    import json

    from fraisier.validation import DeploymentReadinessValidator

    config = ctx.obj["config"]

    fraise_config = config.get_fraise_environment(fraise, environment)
    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    validator = DeploymentReadinessValidator(fraise_config)
    results = validator.run_all()

    all_passed = all(r.passed for r in results if r.severity == "error")
    has_errors = any(not r.passed and r.severity == "error" for r in results)

    if as_json:
        data = {
            "passed": all_passed,
            "checks": [r.to_dict() for r in results],
        }
        click.echo(json.dumps(data, indent=2))
    else:
        # Rich table output
        console.print(
            f"\n[bold]Deployment Readiness: {fraise} / {environment}[/bold]\n"
        )

        table = Table(show_header=False, show_lines=False)
        table.add_column("", style="dim", width=3)
        table.add_column("Check", style="cyan")
        table.add_column("Status", no_wrap=False)

        errors = [r for r in results if not r.passed and r.severity == "error"]
        warnings = [r for r in results if not r.passed and r.severity == "warning"]
        passed = [r for r in results if r.passed]

        for r in passed:
            table.add_row("✓", r.name, f"[green]{r.message or 'OK'}[/green]")

        for r in warnings:
            msg = r.message or "warning"
            table.add_row("⚠", r.name, f"[yellow]{msg}[/yellow]")

        for r in errors:
            msg = r.message or "failed"
            table.add_row("✗", r.name, f"[red]{msg}[/red]")

        console.print(table)

        # Summary line
        summary_parts = []
        if errors:
            summary_parts.append(f"[red]{len(errors)} failed[/red]")
        if warnings:
            summary_parts.append(f"[yellow]{len(warnings)} warning[/yellow]")
        if passed:
            summary_parts.append(f"[green]{len(passed)} passed[/green]")

        status_color = "green" if not has_errors else "red"
        status_text = "READY" if not has_errors else "NOT READY"
        summary_text = f"\nSummary: {', '.join(summary_parts)} → "
        summary_text += f"[{status_color}]{status_text}[/{status_color}]\n"
        console.print(summary_text)

    raise SystemExit(0 if all_passed else 1)

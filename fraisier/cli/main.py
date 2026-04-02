"""Main CLI group and core commands (init, list, deploy, status)."""

from __future__ import annotations

from pathlib import Path

import click
from rich.table import Table
from rich.tree import Tree

from fraisier.config import get_config

from ._helpers import _get_deployer, _print_dry_run, console


@click.group()
@click.version_option(package_name="fraisier", prog_name="fraisier")
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True),
    help="Path to fraises.yaml configuration file",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose/debug output")
@click.pass_context
def main(ctx: click.Context, config: str | None, verbose: bool) -> None:
    """Fraisier - Deployment orchestrator for the FraiseQL ecosystem.

    Manage deployments for all your fraises (services) across multiple providers
    (Bare Metal, Docker Compose).

    \b
    Examples:
        fraisier list
        fraisier deploy my_api production
        fraisier providers
        fraisier provider-info bare_metal
        fraisier provider-test docker_compose -f config.yaml
    """
    if verbose:
        import logging

        logging.basicConfig(format="%(name)s %(levelname)s %(message)s")
        logging.getLogger().setLevel(logging.DEBUG)

    ctx.ensure_object(dict)
    try:
        ctx.obj["config"] = get_config(config)
    except FileNotFoundError:
        ctx.obj["config"] = None
    ctx.obj["skip_health"] = False


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


@main.command()
@click.option("--flat", is_flag=True, help="Show flat list instead of grouped")
@click.pass_context
def list(ctx: click.Context, flat: bool) -> None:
    """List all registered fraises and their environments."""
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
@click.argument("fraise")
@click.argument("environment")
@click.option("--dry-run", is_flag=True, help="Show what would be deployed")
@click.option("--force", is_flag=True, help="Deploy even if versions match")
@click.option("--skip-health", is_flag=True, help="Skip health check after deploy")
@click.option("--job", "-j", help="Specific job name (for scheduled fraises)")
@click.option(
    "--if-changed",
    is_flag=True,
    help="Only deploy if versions differ (quiet, for systemd timers)",
)
@click.option(
    "--no-rollback",
    is_flag=True,
    help="Allow irreversible migrations (skip rollback safety)",
)
@click.pass_context
def deploy(
    ctx: click.Context,
    fraise: str,
    environment: str,
    dry_run: bool,
    force: bool,
    skip_health: bool,
    job: str | None,
    if_changed: bool,
    no_rollback: bool,
) -> None:
    """Deploy a fraise to an environment.

    \b
    FRAISE is the fraise name (e.g., my_api, etl, backup)
    ENVIRONMENT is the target environment (e.g., development, staging, production)

    \b
    Examples:
        fraisier deploy my_api production
        fraisier deploy etl production --dry-run
        fraisier deploy backup production --job local_backup
    """
    config = ctx.obj["config"]
    fraise_config = config.get_fraise_environment(fraise, environment)

    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        console.print("\nAvailable fraises:")
        for f in config.list_fraises_detailed():
            envs = ", ".join(f["environments"])
            console.print(f"  {f['name']}: {envs}")
        raise SystemExit(1)

    fraise_type = fraise_config.get("type")

    ctx.obj["skip_health"] = skip_health

    if dry_run:
        _print_dry_run(config, fraise, environment, fraise_config)
        return

    # Pass --no-rollback flag through to deployer config
    if no_rollback:
        fraise_config["allow_irreversible"] = True

    # Get deployer based on type
    deployer = _get_deployer(fraise_type, fraise_config, job)

    if deployer is None:
        console.print(f"[red]Error:[/red] Unknown fraise type '{fraise_type}'")
        raise SystemExit(1)

    # Check if deployment is needed
    if not force and not deployer.is_deployment_needed():
        if if_changed:
            click.echo("No changes, skipping.")
        else:
            console.print(
                f"[yellow]Fraise '{fraise}/{environment}' "
                f"is already up to date[/yellow]"
            )
            current = deployer.get_current_version()
            console.print(f"Current version: {current}")
        return

    # Execute deployment with lock (file or database backend)
    from fraisier.locking import deployment_lock

    console.print(f"[green]Deploying {fraise} -> {environment}...[/green]")

    try:
        with deployment_lock(fraise):
            result = deployer.execute()
    except Exception as e:
        if "already running" in str(e).lower():
            console.print(f"[red]Error:[/red] Deploy already running for '{fraise}'")
            raise SystemExit(1) from None
        raise

    if result.success:
        console.print("[green]Deployment successful![/green]")
        console.print(f"  Version: {result.old_version} -> {result.new_version}")
        console.print(f"  Duration: {result.duration_seconds:.1f}s")
    else:
        console.print("[red]Deployment failed![/red]")
        console.print(f"  Status: {result.status.value}")
        console.print(f"  Error: {result.error_message}")
        raise SystemExit(1)


@main.command()
@click.argument("fraise", required=False, default=None)
@click.argument("environment", required=False, default=None)
@click.pass_context
def status(ctx: click.Context, fraise: str | None, environment: str | None) -> None:
    """Check status of fraise(s).

    Shows deployment status and health.

    \b
    Examples:
        fraisier status                        # Global view of all fraises
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
        _show_global_status(config)
        return

    # Single fraise view: existing behavior
    _show_single_status(config, fraise, environment)


def _compute_status_string(current: str | None, latest: str | None) -> str:
    """Compute deployment status string based on versions."""
    if current == latest and current is not None:
        return "[green]deployed ✓[/green]"
    if current is None or latest is None:
        return "[dim]unknown[/dim]"
    return "[yellow]out-of-date[/yellow]"


def _compute_health_string(fraise_config: dict, deployer) -> str:
    """Compute health status string based on config and deployer health check."""
    health_check_cfg = fraise_config.get("health_check", {})
    has_health = health_check_cfg.get("url") is not None
    has_timer = fraise_config.get("systemd_timer") is not None

    if not has_health and not has_timer:
        return "[dim]not configured[/dim]"

    health_ok = deployer.health_check()
    return "[green]healthy ✓[/green]" if health_ok else "[red]unhealthy[/red]"


def _show_global_status(config) -> None:
    """Display deployment status table for all fraises/environments."""
    from fraisier.database import get_db

    if config is None:
        console.print("[yellow]No configuration loaded[/yellow]")
        return

    table = Table(title="[bold]Deployment Status[/bold]", expand=True)
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
            status_str = _compute_status_string(current, latest)
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

        except Exception as e:
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


def _show_single_status(config, fraise: str, environment: str) -> None:
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

            # Check if deployment is needed
            needs_deployment = deployer.is_deployment_needed()
            deployment_status = (
                "[yellow]needs update[/yellow]"
                if needs_deployment
                else "[green]up to date[/green]"
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

        except Exception as e:
            console.print(f"\n[red]Error checking status:[/red] {e}")


@main.command()
@click.option("--project", required=True, help="Project name to deploy")
@click.pass_context
def deploy_daemon(ctx: click.Context, project: str) -> None:  # noqa: ARG001
    """Run deployment daemon that reads JSON from stdin.

    This command reads a JSON deployment request from stdin and executes
    the deployment. Used internally by systemd socket activation.

    \b
    Example:
        echo '{"version": 1, "project": "api", "environment": "dev", ...}' | \\
        fraisier deploy-daemon --project=api
    """
    import sys

    from fraisier.daemon import execute_deployment_request, parse_deployment_request

    # Read JSON from stdin
    try:
        json_input = sys.stdin.read().strip()
        if not json_input:
            console.print("[red]Error:[/red] No input received on stdin")
            raise SystemExit(1)
    except Exception as e:
        console.print(f"[red]Error reading stdin:[/red] {e}")
        raise SystemExit(1) from None

    # Parse and validate request
    try:
        request = parse_deployment_request(json_input)
    except ValueError as e:
        console.print(f"[red]Error parsing request:[/red] {e}")
        raise SystemExit(1) from None

    # Validate project matches
    if request.project != project:
        console.print(
            f"[red]Error:[/red] Project mismatch: requested '{request.project}' "
            f"but daemon configured for '{project}'"
        )
        raise SystemExit(1)

    # Execute deployment
    try:
        result = execute_deployment_request(request)
    except Exception as e:
        console.print(f"[red]Error executing deployment:[/red] {e}")
        raise SystemExit(1) from None

    # Exit with appropriate code
    if result.success:
        console.print(f"[green]Deployment successful[/green] - {result.message}")
        if result.deployed_version:
            console.print(f"Version: {result.deployed_version}")
        raise SystemExit(0)
    else:
        console.print(f"[red]Deployment failed[/red] - {result.error_message}")
        raise SystemExit(1)


@main.command()
@click.argument("fraise")
@click.argument("environment")
@click.option("--to-version", default=None, help="Target SHA to roll back to")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def rollback(
    ctx: click.Context,
    fraise: str,
    environment: str,
    to_version: str | None,
    force: bool,
) -> None:
    """Roll back a fraise to its previous version.

    \b
    Looks up the previous successful deployment from history and
    checks out that version.  Use --to-version to target a specific SHA.

    \b
    Examples:
        fraisier rollback my_api production
        fraisier rollback my_api production --to-version abc1234
        fraisier rollback my_api production --force
    """
    config = ctx.obj["config"]
    fraise_config = config.get_fraise_environment(fraise, environment)

    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    fraise_type = fraise_config.get("type")
    deployer = _get_deployer(fraise_type, fraise_config)

    if deployer is None or not hasattr(deployer, "rollback"):
        console.print(f"[red]Deployer for {fraise} does not support rollback[/red]")
        raise SystemExit(1)

    target = to_version

    if not target:
        from fraisier.database import get_db

        db = get_db()
        history = db.get_recent_deployments(
            limit=2, fraise=fraise, environment=environment
        )
        successful = [d for d in history if d["status"] == "success"]
        if len(successful) >= 2:
            target = successful[1].get("new_version")

    if not target:
        console.print("[red]No previous version found to roll back to[/red]")
        console.print("Use --to-version <sha> to specify a target explicitly.")
        raise SystemExit(1)

    current = deployer.get_current_version()

    if not force:
        console.print(f"Will roll back [bold]{fraise}[/bold] ({environment})")
        console.print(f"  From: {current or 'unknown'}")
        console.print(f"  To:   {target[:8]}")
        if not click.confirm("Proceed?"):
            console.print("Aborted.")
            return

    result = deployer.rollback(to_version=target)

    if result.success:
        console.print(f"[green]Rolled back {fraise} to {result.new_version}[/green]")
    else:
        console.print(f"[red]Rollback failed: {result.error_message}[/red]")
        raise SystemExit(1)


# Import submodules to register their commands with `main`
from . import db as _db_mod  # noqa: E402, F401
from . import health as _health_mod  # noqa: E402, F401
from . import ops as _ops_mod  # noqa: E402, F401
from . import providers as _providers_mod  # noqa: E402, F401
from . import scaffold as _scaffold_mod  # noqa: E402, F401
from . import setup as _setup_mod  # noqa: E402, F401
from . import test_components as _test_components_mod  # noqa: E402, F401
from . import test_db as _test_db_mod  # noqa: E402, F401
from . import version as _version_mod  # noqa: E402, F401

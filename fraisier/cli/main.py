"""Main CLI group and core commands (init, list, deploy, status)."""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path

import click
from rich.table import Table
from rich.tree import Tree

from fraisier.config import get_config
from fraisier.status import elapsed_seconds, read_status

from ._helpers import _get_deployer, console


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
        fraisier trigger-deploy my_api production
        fraisier deployment-status my_api
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


def _compute_health_string(fraise_config: dict, deployer) -> str:
    """Compute health status string based on config and deployer health check."""
    health_check_cfg = fraise_config.get("health_check", {})
    has_health = health_check_cfg.get("url") is not None
    has_timer = fraise_config.get("systemd_timer") is not None

    if not has_health and not has_timer:
        return "[dim]not configured[/dim]"

    health_ok = deployer.health_check()
    return "[green]healthy ✓[/green]" if health_ok else "[red]unhealthy[/red]"


def _show_global_status(config, server_filter: str | None = None) -> None:
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
@click.option(
    "--branch",
    default=None,
    help="Git branch to deploy (defaults to configured branch)",
)
@click.option("--force", is_flag=True, help="Force deployment even if up to date")
@click.option("--no-cache", is_flag=True, help="Skip deployment caches")
@click.option("--dry-run", is_flag=True, help="Show deployment plan without executing")
@click.option(
    "--timeout", type=int, default=300, help="Timeout in seconds (default: 300)"
)
@click.pass_context
def trigger_deploy(
    ctx: click.Context,
    fraise: str,
    environment: str,
    branch: str | None,
    force: bool,
    no_cache: bool,
    dry_run: bool,
    timeout: int,
) -> None:
    """Trigger deployment by writing to systemd socket.

    Connects to the deployment socket for the specified fraise and environment,
    sends a JSON deployment request, and waits for completion.

    \b
    Examples:
        fraisier trigger-deploy my_api production
        fraisier trigger-deploy my_api development --branch feature-x
        fraisier trigger-deploy my_api staging --force --timeout 600
    """
    import json
    import socket
    import time
    from pathlib import Path

    config = ctx.obj["config"]

    # Validate fraise/environment exists
    fraise_config = config.get_fraise_environment(fraise, environment)
    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    # Determine branch
    if not branch:
        branch = fraise_config.get("branch", "main")

    # Build socket path: /run/fraisier/{project}-{environment}/deploy.sock
    project_name = config.project_name
    socket_dir = Path("/run/fraisier") / f"{project_name}-{environment}"
    socket_path = socket_dir / "deploy.sock"

    # Build deployment request
    request = {
        "version": 1,
        "project": project_name,
        "environment": environment,
        "branch": branch,
        "timestamp": time.time(),
        "triggered_by": "cli",
        "options": {
            "force": force,
            "no_cache": no_cache,
            "dry_run": dry_run,
        },
        "metadata": {
            "cli_user": ctx.obj.get("user", "unknown"),
        },
    }

    try:
        # Connect to socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(socket_path))

        # Send JSON request
        json_data = json.dumps(request, default=str)
        sock.sendall(json_data.encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)

        # Wait for socket to close (indicates service completion)
        try:
            while True:
                data = sock.recv(1024)
                if not data:
                    break
                # Service may send back data, but we ignore it for now
        except TimeoutError:
            console.print(
                f"[red]Error:[/red] Deployment timed out after {timeout} seconds\n"
                f"[yellow]Hint:[/yellow] The deployment may still be running in the background.\n"  # noqa: E501
                f"  Check status: fraisier deployment-status {fraise}\n"
                f"  For long deployments, increase timeout: --timeout {timeout * 2}"
            )
            raise SystemExit(1) from None

        console.print("[green]✓[/green] Deployment triggered successfully")
        sock.close()

    except FileNotFoundError:
        console.print(
            f"[red]Error:[/red] Deployment socket not found: {socket_path}\n"
            f"[yellow]Hint:[/yellow] Ensure systemd socket is enabled and running:\n"
            f"  systemctl enable fraisier-{project_name}-{environment}-deploy.socket\n"
            f"  systemctl start fraisier-{project_name}-{environment}-deploy.socket"
        )
        raise SystemExit(1) from None
    except ConnectionRefusedError:
        console.print(
            f"[red]Error:[/red] Cannot connect to deployment socket: {socket_path}\n"
            f"[yellow]Hint:[/yellow] Socket service may not be running. Check status:\n"
            f"  systemctl status fraisier-{project_name}-{environment}-deploy.socket"
        )
        raise SystemExit(1) from None
    except PermissionError:
        console.print(
            f"[red]Error:[/red] Permission denied connecting to socket: {socket_path}\n"
            f"[yellow]Hint:[/yellow] Socket is restricted to web user group.\n"
            f"  Check socket ownership: ls -la {socket_path}\n"
            f"  Ensure current user is in web group: groups {ctx.obj.get('user', 'current_user')}\n"  # noqa: E501
            f"  Add to group if needed: sudo usermod -a -G www-data {ctx.obj.get('user', 'current_user')}"  # noqa: E501
        )
        raise SystemExit(1) from None
    except Exception as e:
        console.print(f"[red]Error:[/red] Failed to trigger deployment: {e}")
        raise SystemExit(1) from e


@main.command()
@click.argument("fraise")
@click.option("--json", is_flag=True, help="Output in JSON format")
@click.pass_context
def deployment_status(ctx: click.Context, fraise: str, json: bool) -> None:
    """Show the last deployment status for a fraise.

    Reads the deployment status from the socket-activated daemon's status file
    and displays current deployment information.

    \b
    Examples:
        fraisier deployment-status my_api
        fraisier deployment-status my_api --json
    """
    import json
    from pathlib import Path

    config = ctx.obj["config"]

    # Find the fraise configuration
    fraise_config = config.get_fraise(fraise)
    if not fraise_config:
        console.print(f"[red]Error:[/red] Fraise '{fraise}' not found")
        raise SystemExit(1)

    # Get all environments for this fraise
    environments = list(fraise_config.get("environments", {}).keys())
    if not environments:
        console.print(
            f"[red]Error:[/red] No environments configured for fraise '{fraise}'"
        )
        raise SystemExit(1)

    project_name = config.project_name

    # Try to read status for each environment
    status_found = False
    for env in environments:
        status_path = Path("/run/fraisier") / f"{project_name}-{env}.last_deployment"

        if not status_path.exists():
            continue

        try:
            data = json.loads(status_path.read_text())
            status_found = True

            if json:
                # Output raw JSON
                import sys

                json.dump(data, sys.stdout, indent=2)
                print()  # newline
            else:
                # Human-readable output
                _display_deployment_status(data, env)

        except (json.JSONDecodeError, OSError) as e:
            console.print(f"[red]Error reading status for {env}:[/red] {e}")
            continue

    if not status_found:
        console.print("[yellow]No deployment status found[/yellow]")
        console.print(
            f"[dim]Looked in: /run/fraisier/{project_name}-*.last_deployment[/dim]"
        )
        console.print(
            "[dim]No deployments run yet, or socket activation not configured.[/dim]"
        )


def _display_deployment_status(data: dict, environment: str) -> None:
    """Display deployment status in human-readable format."""
    status_val = data.get("status", "unknown")

    if status_val == "success":
        status_str = "[green]success[/green]"
        icon = "✓"
    elif status_val == "failed":
        status_str = "[red]failed[/red]"
        icon = "✗"
    elif status_val == "in_progress":
        status_str = "[yellow]in progress[/yellow]"
        icon = "⋯"
    elif status_val == "queued":
        status_str = "[blue]queued[/blue]"
        icon = "⋯"
    else:
        status_str = f"[yellow]{status_val}[/yellow]"
        icon = "?"

    console.print(f"[bold]Project:[/bold]     {data.get('project', '?')}")
    console.print(f"[bold]Environment:[/bold] {environment}")
    console.print(f"[bold]Status:[/bold]       {status_str} {icon}")

    deployed_version = data.get("deployed_version")
    if deployed_version:
        deployed_at = data.get("deployed_at", "")
        if deployed_at:
            console.print(
                f"[bold]Deployed:[/bold]     {deployed_version} ({deployed_at})"
            )
        else:
            console.print(f"[bold]Deployed:[/bold]     {deployed_version}")

    latest_version = data.get("latest_version")
    if latest_version and latest_version != deployed_version:
        console.print(f"[bold]Available:[/bold]    {latest_version}")

    health = data.get("health_check_status")
    if health:
        if health == "healthy":
            health_str = "[green]healthy ✓[/green]"
        else:
            health_str = f"[red]{health} ✗[/red]"
        console.print(f"[bold]Health Check:[/bold] {health_str}")

    duration = data.get("duration_seconds")
    if duration is not None:
        console.print(f"[bold]Duration:[/bold]     {duration:.1f}s")

    error = data.get("error")
    if error:
        console.print(f"[bold]Error:[/bold]        {error}")


@main.command()
@click.argument("fraise")
@click.option("--json", is_flag=True, help="Output validation results in JSON format")
@click.pass_context
def validate_setup(ctx: click.Context, fraise: str, json: bool) -> None:
    """Validate socket activation setup for a fraise.

    Checks systemd version, socket paths, permissions, and unit files
    to ensure socket activation is properly configured.

    \b
    Examples:
        fraisier validate-setup my_api
        fraisier validate-setup my_api --json
    """
    import json as json_module
    from pathlib import Path

    config = ctx.obj["config"]

    # Find the fraise configuration
    fraise_config = config.get_fraise(fraise)
    if not fraise_config:
        console.print(f"[red]Error:[/red] Fraise '{fraise}' not found")
        raise SystemExit(1)

    # Get all environments for this fraise
    environments = list(fraise_config.get("environments", {}).keys())
    if not environments:
        console.print(
            f"[red]Error:[/red] No environments configured for fraise '{fraise}'"
        )
        raise SystemExit(1)

    project_name = config.project_name
    validation_results = {}

    # Check systemd version
    systemd_ok, systemd_version, systemd_msg = _check_systemd_version()
    validation_results["systemd"] = {
        "ok": systemd_ok,
        "version": systemd_version,
        "message": systemd_msg,
    }

    # Check each environment
    env_results = {}
    for env in environments:
        socket_dir = Path("/run/fraisier") / f"{project_name}-{env}"
        socket_path = socket_dir / "deploy.sock"

        env_checks = {
            "socket_directory": _check_socket_directory(socket_dir),
            "socket_file": _check_socket_file(socket_path),
            "socket_permissions": _check_socket_permissions(socket_path),
            "systemd_units": _check_systemd_units(project_name, env),
            "user_permissions": _check_user_permissions(socket_path),
        }
        env_results[env] = env_checks

    validation_results["environments"] = env_results

    # Overall status
    all_ok = systemd_ok and all(
        all(check["ok"] for check in env_checks.values())
        for env_checks in env_results.values()
    )

    if json:
        # JSON output
        output = {
            "fraise": fraise,
            "overall_status": "ok" if all_ok else "issues_found",
            **validation_results,
        }
        import sys

        json_module.dump(output, sys.stdout, indent=2)
        print()
    else:
        # Human-readable output
        console.print(f"[bold]Validating socket activation setup for '{fraise}'[/bold]")
        console.print()

        # Systemd check
        status_icon = "✓" if systemd_ok else "✗"
        color = "green" if systemd_ok else "red"
        console.print(f"[{color}]Systemd: {systemd_msg} {status_icon}[/{color}]")

        # Environment checks
        for env, checks in env_results.items():
            console.print(f"[bold]Environment: {env}[/bold]")

            for check_name, result in checks.items():
                status_icon = "✓" if result["ok"] else "✗"
                color = "green" if result["ok"] else "red"
                msg = f"{check_name}: {result['message']} {status_icon}"
                console.print(f"  [{color}]{msg}[/{color}]")

        console.print()
        if all_ok:
            console.print("[green]✓ All validation checks passed![/green]")
        else:
            console.print(
                "[yellow]⚠ Some validation checks failed. "
                "Run 'fraisier diagnose' for troubleshooting.[/yellow]"
            )
            raise SystemExit(1)


def _check_systemd_version() -> tuple[bool, str, str]:
    """Check if systemd version meets requirements (>= 230)."""
    try:
        import subprocess

        result = subprocess.run(
            ["systemctl", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            # First line: "systemd 249 (249.7-1-arch)"
            first_line = result.stdout.split("\n")[0]
            version_str = first_line.split()[1]  # Extract version number
            try:
                version = int(version_str.split(".")[0])  # Major version
                if version >= 230:
                    return True, version_str, f"systemd {version_str} (compatible)"
                else:
                    return (
                        False,
                        version_str,
                        f"systemd {version_str} (requires >= 230)",
                    )
            except ValueError:
                return (
                    False,
                    version_str,
                    f"systemd {version_str} (unable to parse version)",
                )
        else:
            return False, "unknown", "systemctl command failed"
    except (subprocess.SubprocessError, FileNotFoundError):
        return False, "unknown", "systemd not available"


def _check_socket_directory(socket_dir: Path) -> dict:
    """Check if socket directory exists and has correct permissions."""
    if not socket_dir.exists():
        return {"ok": False, "message": f"Directory {socket_dir} does not exist"}

    # Check permissions (should be 755 or similar)
    try:
        stat = socket_dir.stat()
        mode = stat.st_mode & 0o777
        if mode >= 0o755:  # Owner can read/write/execute, group/others can read/execute
            return {
                "ok": True,
                "message": f"Directory exists with permissions {oct(mode)}",
            }
        else:
            return {
                "ok": False,
                "message": f"Directory permissions {oct(mode)} too restrictive",
            }
    except OSError as e:
        return {"ok": False, "message": f"Cannot check directory permissions: {e}"}


def _check_socket_file(socket_path: Path) -> dict:
    """Check if socket file exists."""
    if socket_path.exists():
        return {"ok": True, "message": f"Socket file exists at {socket_path}"}
    else:
        return {"ok": False, "message": f"Socket file does not exist at {socket_path}"}


def _check_socket_permissions(socket_path: Path) -> dict:
    """Check socket file permissions."""
    if not socket_path.exists():
        return {"ok": False, "message": "Socket file does not exist"}

    try:
        stat = socket_path.stat()
        mode = stat.st_mode & 0o777

        # Socket files should be accessible to web group
        # Typically 660 (owner and group can read/write)
        if mode >= 0o660:
            return {"ok": True, "message": f"Socket has permissions {oct(mode)}"}
        else:
            return {
                "ok": False,
                "message": f"Socket permissions {oct(mode)} too restrictive",
            }
    except OSError as e:
        return {"ok": False, "message": f"Cannot check socket permissions: {e}"}


def _check_systemd_units(project_name: str, environment: str) -> dict:
    """Check if systemd units are installed and enabled."""
    unit_name = f"fraisier-{project_name}-{environment}-deploy.socket"

    try:
        import subprocess

        # Check if unit file exists
        result = subprocess.run(
            ["systemctl", "cat", unit_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return {"ok": False, "message": f"Systemd unit {unit_name} not found"}

        # Check if unit is enabled
        result = subprocess.run(
            ["systemctl", "is-enabled", unit_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and "enabled" in result.stdout:
            return {
                "ok": True,
                "message": f"Systemd unit {unit_name} is installed and enabled",
            }
        else:
            return {"ok": False, "message": f"Systemd unit {unit_name} not enabled"}

    except (subprocess.SubprocessError, FileNotFoundError):
        return {"ok": False, "message": "systemctl command not available"}


def _check_user_permissions(socket_path: Path) -> dict:
    """Check if current user can access the socket."""
    if not socket_path.exists():
        return {"ok": False, "message": "Socket file does not exist"}

    try:
        # Try to get socket file info
        stat = socket_path.stat()
        import grp
        import pwd

        # Get current user
        current_uid = pwd.getpwuid(os.getuid()).pw_uid
        current_user = pwd.getpwuid(os.getuid()).pw_name

        # Get socket owner/group
        socket_uid = stat.st_uid
        socket_gid = stat.st_gid

        # Check if user is owner or in group
        user_groups = [g.gr_gid for g in grp.getgrall() if current_user in g.gr_mem]
        user_groups.append(os.getgid())  # Primary group

        if current_uid == socket_uid or socket_gid in user_groups:
            return {"ok": True, "message": f"User {current_user} can access socket"}
        else:
            socket_group = grp.getgrgid(socket_gid).gr_name
            return {
                "ok": False,
                "message": f"User {current_user} not in socket group '{socket_group}'",
            }

    except (OSError, KeyError) as e:
        return {"ok": False, "message": f"Cannot check user permissions: {e}"}


@main.command()
@click.argument("fraise")
@click.argument("environment")
@click.option("--json", is_flag=True, help="Output diagnostic results in JSON format")
@click.pass_context
def diagnose(ctx: click.Context, fraise: str, environment: str, json: bool) -> None:
    _diagnose(ctx, fraise, environment, json)


def _diagnose(ctx: click.Context, fraise: str, environment: str, json: bool) -> None:
    """Diagnose deployment issues for a fraise environment.

    Analyzes recent deployment logs, status files, and socket connectivity
    to identify issues and provide actionable troubleshooting steps.

    \b
    Examples:
        fraisier diagnose my_api production
        fraisier diagnose my_api development --json
    """
    config = ctx.obj["config"]

    fraise_config = config.get_fraise_environment(fraise, environment)
    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    project_name = config.project_name
    run_dir = Path("/run/fraisier")
    socket_path = run_dir / f"{project_name}-{environment}" / "deploy.sock"
    status_path = run_dir / f"{project_name}-{environment}.last_deployment"
    service_name = fraise_config.get("systemd_service")
    socket_unit = f"fraisier-{project_name}-{environment}-deploy.socket"

    socket_check = _diagnose_socket_connectivity(socket_path)
    status_check = _diagnose_deployment_status(status_path)
    systemd_check = _diagnose_systemd_service(service_name)
    socket_unit_check = _diagnose_systemd_socket_unit(socket_unit)

    issues_found, suggestions = _build_diagnostic_issues(
        socket_check,
        status_check,
        systemd_check,
        socket_unit_check,
        service_name,
        socket_unit,
        socket_path,
        ctx,
        fraise,
    )

    diagnostic_results = {
        "socket_connectivity": socket_check,
        "recent_deployment": status_check,
        "systemd_service": systemd_check,
        "socket_unit": socket_unit_check,
        "issues_found": issues_found,
        "suggestions": suggestions,
    }

    _output_diagnose_results(
        json,
        fraise,
        environment,
        diagnostic_results,
        status_check,
        issues_found,
        suggestions,
    )


def _build_diagnostic_issues(
    socket_check: dict,
    status_check: dict,
    systemd_check: dict,
    socket_unit_check: dict,
    service_name: str,
    socket_unit: str,
    socket_path: Path,
    ctx: click.Context,
    fraise: str,
) -> tuple[list[str], list[dict]]:
    issues_found: list[str] = []
    suggestions: list[dict] = []

    if not socket_check["can_connect"]:
        issues_found.append("socket_connectivity")
        if socket_check["socket_exists"]:
            suggestions.append(
                {
                    "issue": "Socket exists but cannot connect",
                    "fixes": [
                        f"Check socket permissions: ls -la {socket_path}",
                        f"Verify user is in socket group: groups "
                        f"{ctx.obj.get('user', 'current_user')}",
                        f"Check systemd socket status: systemctl status {socket_unit}",
                        f"Restart socket unit: sudo systemctl restart {socket_unit}",
                    ],
                }
            )
        else:
            suggestions.append(
                {
                    "issue": "Socket file does not exist",
                    "fixes": [
                        f"Enable socket unit: sudo systemctl enable {socket_unit}",
                        f"Start socket unit: sudo systemctl start {socket_unit}",
                        f"Check socket unit file: systemctl cat {socket_unit}",
                    ],
                }
            )

    if status_check["status"] == "failed":
        issues_found.append("recent_deployment")
        error_msg = status_check.get("error", "Unknown error")
        suggestions.append(
            {
                "issue": f"Recent deployment failed: {error_msg}",
                "fixes": [
                    f"Check deployment logs: journalctl -u {service_name} -n 50",
                    "Verify app configuration in fraises.yaml",
                    f"Test service manually: sudo systemctl start {service_name}",
                    f"Check app logs in /opt/{fraise}/logs/",
                ],
            }
        )

    if not systemd_check["service_exists"]:
        issues_found.append("systemd_service")
        suggestions.append(
            {
                "issue": f"Systemd service {service_name} not found",
                "fixes": [
                    f"Install service unit: sudo cp "
                    f"scripts/generated/systemd/{service_name} /etc/systemd/system/",
                    "Reload systemd: sudo systemctl daemon-reload",
                    f"Enable service: sudo systemctl enable {service_name}",
                ],
            }
        )
    elif not systemd_check["service_running"]:
        issues_found.append("systemd_service")
        suggestions.append(
            {
                "issue": f"Systemd service {service_name} not running",
                "fixes": [
                    f"Check service status: systemctl status {service_name}",
                    f"Start service: sudo systemctl start {service_name}",
                    f"Check service logs: journalctl -u {service_name} -n 20",
                ],
            }
        )

    if not socket_unit_check["unit_exists"]:
        issues_found.append("socket_unit")
        suggestions.append(
            {
                "issue": f"Socket unit {socket_unit} not found",
                "fixes": [
                    f"Install socket unit: sudo cp "
                    f"scripts/generated/systemd/{socket_unit} /etc/systemd/system/",
                    "Reload systemd: sudo systemctl daemon-reload",
                    f"Enable socket: sudo systemctl enable {socket_unit}",
                ],
            }
        )
    elif not socket_unit_check["unit_active"]:
        issues_found.append("socket_unit")
        suggestions.append(
            {
                "issue": f"Socket unit {socket_unit} not active",
                "fixes": [
                    f"Check socket status: systemctl status {socket_unit}",
                    f"Start socket: sudo systemctl start {socket_unit}",
                    f"Check socket logs: journalctl -u {socket_unit} -n 20",
                ],
            }
        )

    return issues_found, suggestions


def _output_diagnose_results(
    json_flag: bool,
    fraise: str,
    environment: str,
    diagnostic_results: dict,
    status_check: dict,
    issues_found: list[str],
    suggestions: list[dict],
) -> None:
    if json_flag:
        import json as json_module
        import sys

        output = {
            "fraise": fraise,
            "environment": environment,
            "diagnostics": diagnostic_results,
        }
        json_module.dump(output, sys.stdout, indent=2)
        print()
        return

    console.print(f"[bold]Diagnosing issues for '{fraise}' / '{environment}'[/bold]")
    console.print()

    if not issues_found:
        console.print("[green]✓ No deployment issues detected[/green]")
        console.print()
        console.print("Recent deployment status:")
        if status_check["status"]:
            console.print(f"  Status: {status_check['status']}")
            if status_check.get("deployed_version"):
                console.print(f"  Version: {status_check['deployed_version']}")
            if status_check.get("deployed_at"):
                console.print(f"  Deployed: {status_check['deployed_at']}")
        else:
            console.print("  No recent deployments found")
    else:
        console.print(f"[red]⚠ Found {len(issues_found)} potential issue(s):[/red]")
        console.print()

        for i, suggestion in enumerate(suggestions, 1):
            console.print(f"[bold]{i}. {suggestion['issue']}[/bold]")
            console.print("   Suggested fixes:")
            for fix in suggestion["fixes"]:
                console.print(f"     • {fix}")
            console.print()

        console.print(
            "[yellow]Run 'fraisier validate-setup' to check prerequisites.[/yellow]"
        )


def _diagnose_socket_connectivity(socket_path: Path) -> dict:
    """Test if socket is accepting connections."""
    result = {
        "socket_exists": socket_path.exists(),
        "can_connect": False,
        "error": None,
    }

    if not socket_path.exists():
        return result

    try:
        import socket as socket_module

        sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(str(socket_path))
        sock.close()
        result["can_connect"] = True
    except (OSError, ConnectionRefusedError) as e:
        result["error"] = str(e)

    return result


def _diagnose_deployment_status(status_path: Path) -> dict:
    """Analyze recent deployment status."""
    result = {
        "status_file_exists": status_path.exists(),
        "status": None,
        "deployed_version": None,
        "deployed_at": None,
        "error": None,
    }

    if not status_path.exists():
        return result

    try:
        import json as json_module

        data = json_module.loads(status_path.read_text())
        result.update(
            {
                "status": data.get("status"),
                "deployed_version": data.get("deployed_version"),
                "deployed_at": data.get("deployed_at"),
                "error": data.get("error"),
            }
        )
    except (OSError, json_module.JSONDecodeError) as e:
        result["error"] = f"Cannot read status file: {e}"

    return result


def _diagnose_systemd_service(service_name: str) -> dict:
    """Check systemd service status."""
    if not service_name:
        return {"service_name": None, "service_exists": False, "service_running": False}

    result = {
        "service_name": service_name,
        "service_exists": False,
        "service_running": False,
        "error": None,
    }

    try:
        import subprocess

        # Check if service exists
        check_result = subprocess.run(
            ["systemctl", "cat", service_name],
            capture_output=True,
            timeout=5,
            check=False,
        )
        result["service_exists"] = check_result.returncode == 0

        if result["service_exists"]:
            # Check if service is running
            status_result = subprocess.run(
                ["systemctl", "is-active", service_name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            result["service_running"] = "active" in status_result.stdout

    except (subprocess.SubprocessError, FileNotFoundError) as e:
        result["error"] = str(e)

    return result


def _diagnose_systemd_socket_unit(unit_name: str) -> dict:
    """Check systemd socket unit status."""
    result = {
        "unit_name": unit_name,
        "unit_exists": False,
        "unit_active": False,
        "error": None,
    }

    try:
        import subprocess

        # Check if unit exists
        check_result = subprocess.run(
            ["systemctl", "cat", unit_name], capture_output=True, timeout=5, check=False
        )
        result["unit_exists"] = check_result.returncode == 0

        if result["unit_exists"]:
            # Check if unit is active
            status_result = subprocess.run(
                ["systemctl", "is-active", unit_name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            result["unit_active"] = "active" in status_result.stdout

    except (subprocess.SubprocessError, FileNotFoundError) as e:
        result["error"] = str(e)

    return result


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
from . import bootstrap as _bootstrap_mod  # noqa: E402, F401
from . import db as _db_mod  # noqa: E402, F401
from . import health as _health_mod  # noqa: E402, F401
from . import ops as _ops_mod  # noqa: E402, F401
from . import providers as _providers_mod  # noqa: E402, F401
from . import scaffold as _scaffold_mod  # noqa: E402, F401
from . import setup as _setup_mod  # noqa: E402, F401
from . import test_components as _test_components_mod  # noqa: E402, F401
from . import test_db as _test_db_mod  # noqa: E402, F401
from . import version as _version_mod  # noqa: E402, F401

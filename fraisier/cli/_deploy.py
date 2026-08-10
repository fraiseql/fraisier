"""Deployment CLI commands: deploy_daemon, trigger_deploy, deployment_status."""

from __future__ import annotations

import os
import time
from pathlib import Path

import click

from ._helpers import console
from .main import main


def _failure_result(error: str) -> dict[str, object]:
    """A machine-readable result for a path that never reached a deployment.

    Same six keys as the success payload, so a client parses one shape whatever
    happened. The reason goes in ``error`` verbatim: these are the paths an
    operator most needs named, and "failed" on its own names nothing.
    """
    return {
        "success": False,
        "status": "failed",
        "version": None,
        "duration": None,
        "error": error,
        "message": None,
    }


def _write_to_accepted_connection(result_json: str) -> None:
    """Write the result to the socket systemd accepted on fd 0, if there is one.

    Under ``Accept=yes`` + ``StandardInput=socket`` fd 0 *is* the client
    connection, so this is the only channel that reaches a waiting
    ``trigger-deploy``. ``StandardOutput=journal`` is deliberate (#72 Bug 2),
    which is why stdout cannot carry the result.

    Never raises. ``deploy-checker.service.j2`` triggers deploys *without*
    ``--wait`` and closes at once, so half an hour later the peer is normally
    gone — a departed client must not turn a successful deployment into a
    failed unit.
    """
    import socket
    import stat

    try:
        if not stat.S_ISSOCK(os.fstat(0).st_mode):
            return  # the documented pipe form; stdout already carried it
    except OSError:
        return  # fd 0 is closed — there is nobody to answer

    try:
        # os.dup(0), never socket.socket(fileno=0): the latter *owns* fd 0 and
        # closes it when collected, out from under a deployment reading stdin.
        with socket.socket(fileno=os.dup(0)) as conn:
            conn.sendall(result_json.encode("utf-8"))
    except OSError as e:
        console.print(f"[yellow]Note:[/yellow] no client received the result: {e}")


def _emit_result(payload: dict[str, object]) -> None:
    """Send the deployment result everywhere it is owed.

    stdout keeps the record the journal has always held; the accepted
    connection is what a ``--wait`` client reads.
    """
    import json

    result_json = json.dumps(payload)
    print(result_json)  # print() not console.print(): machine-readable output
    _write_to_accepted_connection(result_json)


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

    # Read JSON from stdin. Every terminating path below reports a result on
    # the connection first — a caller that asked for an outcome gets one, and
    # these five are the paths that used to answer with nothing at all (#356).
    try:
        json_input = sys.stdin.read().strip()
    except Exception as e:
        _emit_result(_failure_result(f"Error reading stdin: {e}"))
        console.print(f"[red]Error reading stdin:[/red] {e}")
        raise SystemExit(1) from None

    if not json_input:
        _emit_result(_failure_result("No input received on stdin"))
        console.print("[red]Error:[/red] No input received on stdin")
        raise SystemExit(1)

    # Parse and validate request
    try:
        request = parse_deployment_request(json_input)
    except ValueError as e:
        _emit_result(_failure_result(f"Error parsing request: {e}"))
        console.print(f"[red]Error parsing request:[/red] {e}")
        raise SystemExit(1) from None

    # Validate project matches
    if request.project != project:
        mismatch = (
            f"Project mismatch: requested '{request.project}' "
            f"but daemon configured for '{project}'"
        )
        _emit_result(_failure_result(mismatch))
        console.print(f"[red]Error:[/red] {mismatch}")
        raise SystemExit(1)

    # Execute deployment
    try:
        result = execute_deployment_request(request)
    except Exception as e:
        _emit_result(_failure_result(f"Error executing deployment: {e}"))
        console.print(f"[red]Error executing deployment:[/red] {e}")
        raise SystemExit(1) from None

    _emit_result(
        {
            "success": result.success,
            "status": result.status,
            "version": result.deployed_version,
            "duration": result.duration_seconds,
            "error": result.error_message,
            "message": result.message,
        }
    )

    # Also write human-readable output to stderr for systemd logs
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
    "--wait",
    is_flag=True,
    help="Wait for deployment to finish and print the result summary",
)
@click.option(
    "--follow",
    is_flag=True,
    help="Wait and stream live service logs via journalctl (implies --wait)",
)
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
    wait: bool,
    follow: bool,
    timeout: int,
) -> None:
    """Trigger deployment by writing to systemd socket.

    Connects to the deployment socket for the specified fraise and environment,
    sends a JSON deployment request, and optionally waits for completion.

    \b
    Examples:
        fraisier trigger-deploy my_api production
        fraisier trigger-deploy my_api development --branch feature-x
        fraisier trigger-deploy my_api staging --force --timeout 600
        fraisier trigger-deploy my_api production --wait
        fraisier trigger-deploy my_api production --follow
    """
    import json
    import socket

    config = ctx.obj["config"]

    # Validate fraise/environment exists
    fraise_config = config.get_fraise_environment(fraise, environment)
    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    # Print duration estimate before dispatch (#201 follow-up). Best-effort:
    # any failure (no DB, no history, importable but raises) is swallowed so
    # the deploy still goes through.
    try:
        from fraisier import duration_estimate
        from fraisier.database import get_db

        estimate = duration_estimate.build_estimate(
            get_db(), fraise_config, fraise, environment
        )
        if estimate is not None:
            console.print(duration_estimate.format_estimate_line(estimate))
    except Exception:
        pass

    # Validate flags
    if follow:
        wait = True  # --follow implies --wait

    # Determine branch
    if not branch:
        branch = fraise_config.get("branch", "main")

    # Build socket path using deploy_socket_name() as the single source of truth
    from fraisier.naming import deploy_socket_name

    socket_unit = deploy_socket_name(fraise_config, environment, fraise)
    socket_stem = socket_unit.removesuffix(".socket")
    socket_dir = Path("/run/fraisier") / socket_stem
    socket_path = socket_dir / "deploy.sock"

    # Build deployment request
    request = {
        "version": 1,
        "project": fraise,
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

        # Read response if waiting
        response_data = b""
        if wait or follow:
            try:
                while True:
                    data = sock.recv(4096)
                    if not data:
                        break
                    response_data += data
            except TimeoutError:
                console.print(
                    f"[red]Error:[/red] Deployment timed out after {timeout} seconds\n"
                    f"[yellow]Hint:[/yellow] The deployment may still be running in the background.\n"  # noqa: E501
                    f"  Check status: fraisier deployment-status {fraise}\n"
                    f"  For long deployments, increase timeout: --timeout {timeout * 2}"
                )
                raise SystemExit(1) from None

        # Handle wait/follow modes
        if follow:
            # Exec into journalctl to follow logs.
            # socket_stem was derived from deploy_socket_name() above.
            unit_pattern = f"{socket_stem}@*.service"
            os.execvp("journalctl", ["journalctl", "-u", unit_pattern, "-f"])
        elif wait and response_data:
            # Parse and display result
            from fraisier._output import success

            try:
                result = json.loads(response_data.decode("utf-8"))
                if result["success"]:
                    msg = f"Deployment successful - {result.get('message', '')}"
                    success(msg)
                    if result.get("version"):
                        console.print(f"Version: {result['version']}")
                    if result.get("duration"):
                        console.print(f"Duration: {result['duration']:.1f}s")
                    sock.close()
                    raise SystemExit(0)
                else:
                    console.print(
                        f"[red]✗[/red] Deployment failed - {result.get('error', '')}"
                    )
                    sock.close()
                    raise SystemExit(1)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                console.print(
                    f"[yellow]Warning:[/yellow] Could not parse deployment result: {e}"
                )
                success("Deployment triggered successfully")
        else:
            from fraisier._output import success

            success("Deployment triggered successfully")

        sock.close()

    except FileNotFoundError:
        console.print(
            f"[red]Error:[/red] Deployment socket not found: {socket_path}\n"
            f"[yellow]Hint:[/yellow] Ensure systemd socket is enabled and running:\n"
            f"  systemctl enable fraisier-{fraise}-{environment}-deploy.socket\n"
            f"  systemctl start fraisier-{fraise}-{environment}-deploy.socket"
        )
        raise SystemExit(1) from None
    except ConnectionRefusedError:
        console.print(
            f"[red]Error:[/red] Cannot connect to deployment socket: {socket_path}\n"
            f"[yellow]Hint:[/yellow] Socket service may not be running. Check status:\n"
            f"  systemctl status {socket_stem}.socket"
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
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text)",
)
@click.option(
    "--json",
    "legacy_json",
    is_flag=True,
    hidden=True,
    help="Deprecated alias for --format json",
)
@click.pass_context
def deployment_status(
    ctx: click.Context, fraise: str, fmt: str, legacy_json: bool
) -> None:
    """Show the last deployment status for a fraise.

    Reads the deployment status from the socket-activated daemon's status file
    and displays current deployment information.

    \b
    Examples:
        fraisier deployment-status my_api
        fraisier deployment-status my_api --format json
    """
    import json

    from fraisier.cli._json import resolve_format

    fmt = resolve_format(fmt, legacy_json, command_name="deployment-status")
    json_output = fmt == "json"

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
    permission_denied = False
    for env in environments:
        status_path = Path("/run/fraisier") / f"{project_name}-{env}.last_deployment"

        try:
            if not status_path.exists():
                continue

            data = json.loads(status_path.read_text())
            status_found = True

            if json_output:
                # Output raw JSON
                import sys

                json.dump(data, sys.stdout, indent=2)
                print()  # newline
            else:
                # Human-readable output
                _display_deployment_status(data, env)

        except PermissionError:
            permission_denied = True
            continue
        except (json.JSONDecodeError, OSError) as e:
            console.print(f"[red]Error reading status for {env}:[/red] {e}")
            continue

    if not status_found and permission_denied:
        console.print("[yellow]Permission denied reading deployment status[/yellow]")
        console.print(
            f"[dim]/run/fraisier/{project_name}-*.last_deployment is readable "
            "only by the deploy user.[/dim]"
        )
        console.print(
            "[dim]Re-run as the deploy user (or with sudo) to see the status.[/dim]"
        )
        raise SystemExit(1)

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

    # Show how stale this status is
    deployed_at = data.get("deployed_at", "")
    if deployed_at:
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(deployed_at)
            ago = datetime.now(tz=dt.tzinfo) - dt
            minutes = int(ago.total_seconds() // 60)
            if minutes < 1:
                age_str = "just now"
            elif minutes < 60:
                age_str = f"{minutes}m ago"
            elif minutes < 1440:
                age_str = f"{minutes // 60}h ago"
            else:
                age_str = f"{minutes // 1440}d ago"
            console.print(f"[bold]Last deploy:[/bold]  {deployed_at} ({age_str})")
        except (ValueError, TypeError):
            pass

    error = data.get("error")
    if error:
        console.print(f"[bold]Error:[/bold]        {error}")

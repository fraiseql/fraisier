"""Logs command for tailing systemd journal."""

from __future__ import annotations

import subprocess
import sys

import click

from fraisier import ssh
from fraisier.cli._helpers import console, require_config
from fraisier.cli.main import main


def _resolve_unit_pattern(
    config,
    fraise: str,
    environment: str,
    env_config: dict,
    service: str,
) -> str:
    """Resolve the systemd unit pattern for a fraise service.

    Uses the same naming logic as the scaffold so the pattern matches the
    installed units on every existing deployment.

    Args:
        config: Fraisier config (provides project_name)
        fraise: Fraise name
        environment: Environment key
        env_config: Merged fraise+environment config dict
        service: "deploy" for the deploy daemon, "app" for the main service

    Returns:
        Glob-style unit pattern suitable for ``journalctl -u``.
    """
    from fraisier.naming import app_service_name, deploy_socket_name

    if service == "deploy":
        socket = deploy_socket_name(env_config, environment, fraise)
        stem = socket.removesuffix(".socket")
        return f"{stem}@*.service"

    return app_service_name(config.project_name, fraise, environment, env_config)


@main.command()
@click.argument("fraise")
@click.argument("environment")
@click.option("--no-follow", is_flag=True, help="Don't follow, just dump")
@click.option("--lines", "-n", default=50, help="Number of lines to show")
@click.option("--since", default=None, help="Show logs since (e.g. '10 minutes ago')")
@click.option(
    "--service",
    type=click.Choice(["app", "deploy"]),
    default="deploy",
    help="Which service to tail: 'app' (main service) or 'deploy' (deploy daemon)",
)
@click.pass_context
def logs(
    ctx: click.Context,
    fraise: str,
    environment: str,
    no_follow: bool,
    lines: int,
    since: str | None,
    service: str,
) -> None:
    """Tail systemd journal logs for a fraise service.

    Automatically detects whether the target environment is local or remote.
    For remote environments (those with an ``ssh:`` configuration), connects
    via SSH and runs journalctl on the remote host.

    By default follows logs in real-time. Use --no-follow to dump and exit.

    \b
    Examples:
        fraisier logs api production                          # follow deploy logs
        fraisier logs api production --service app           # follow app logs
        fraisier logs api production --no-follow             # dump last 50 lines
        fraisier logs api production --lines 100             # last 100 lines
        fraisier logs api production --since "1 hour ago"   # logs from last hour
    """
    config = require_config(ctx)

    # Validate fraise/environment exists
    fraise_config = config.get_fraise_environment(fraise, environment)
    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    # Build unit pattern using the same naming logic as the scaffold
    unit_pattern = _resolve_unit_pattern(
        config, fraise, environment, fraise_config, service
    )

    # Build journalctl argument list.
    # Why: commit c9f64e7 — --no-pager is required even on non-TTY stdout:
    # some systemd versions still try to invoke a pager and block until
    # stdin closes.
    jctl_args = ["journalctl", "--no-pager", "-u", unit_pattern, "-n", str(lines)]
    if not no_follow:
        jctl_args.append("-f")
    if since:
        jctl_args.extend(["--since", since])

    # Detect remote vs local. Remote routes through fraisier.ssh.long_stream,
    # which carries the full defensive flag set (BatchMode, ConnectTimeout,
    # StrictHostKeyChecking, AddressFamily, -n, stdin=DEVNULL) by construction
    # — see fraisier/ssh.py and the inventory in
    # .phases/2026-04-10-ssh-io-contract/. Local journalctl needs the same
    # stdin=DEVNULL discipline so background callers don't inherit a pipe
    # that prevents the child from exiting (commits 8fc8fec, 08265c9).
    ssh_config = fraise_config.get("ssh")
    if ssh_config:
        target = ssh.SshTarget.from_config(ssh_config)
        proc = ssh.long_stream(target, jctl_args)
    else:
        proc = subprocess.Popen(jctl_args, stdin=subprocess.DEVNULL)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
    sys.exit(proc.returncode)

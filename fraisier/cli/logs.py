"""Logs command for tailing systemd journal."""

from __future__ import annotations

import os
import shlex

import click

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


def _build_ssh_cmd(ssh_config: dict) -> list[str]:
    """Build the SSH command prefix from a fraise ssh: config block.

    Args:
        ssh_config: Dict with host, user, port, key_path, strict_host_key.

    Returns:
        List starting with "ssh" and ending with "user@host".
    """
    host_key_policy = "accept-new" if ssh_config.get("strict_host_key", True) else "no"
    cmd = [
        "ssh",
        "-o",
        f"StrictHostKeyChecking={host_key_policy}",
        "-o",
        "BatchMode=yes",
        "-p",
        str(ssh_config.get("port", 22)),
    ]
    if key_path := ssh_config.get("key_path"):
        cmd.extend(["-i", key_path])
    cmd.append(f"{ssh_config.get('user', 'root')}@{ssh_config['host']}")
    return cmd


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

    # Build journalctl argument list
    jctl_args = ["journalctl", "-u", unit_pattern, "-n", str(lines)]
    if not no_follow:
        jctl_args.append("-f")
    if since:
        jctl_args.extend(["--since", since])

    # Detect remote vs local
    ssh_config = fraise_config.get("ssh")
    if not ssh_config:
        # Local: replace this process with journalctl
        os.execvp("journalctl", jctl_args)
    else:
        # Remote: SSH to the target server and run journalctl there.
        # os.execvp replaces this process so the TTY is inherited — colour
        # output, Ctrl-C propagation, and --follow all work correctly.
        ssh_prefix = _build_ssh_cmd(ssh_config)
        full_cmd = [*ssh_prefix, shlex.join(jctl_args)]
        os.execvp("ssh", full_cmd)

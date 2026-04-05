"""validate-remote command — check deployment readiness on the target server."""

from __future__ import annotations

import json

import click
from rich.table import Table

from ._helpers import console, require_config
from .bootstrap import _resolve_server_and_runner
from .main import main


@main.command(name="validate-remote")
@click.argument("fraise")
@click.argument("environment")
@click.option(
    "--ssh-user",
    default=None,
    help="SSH user for initial connection (default: ~/.ssh/config or root)",
)
@click.option(
    "--ssh-port",
    default=None,
    type=int,
    help="SSH port (default: from ~/.ssh/config or 22)",
)
@click.option(
    "--ssh-key",
    default=None,
    type=click.Path(),
    help="Path to SSH private key (default: from ~/.ssh/config)",
)
@click.option(
    "--server",
    default=None,
    help="Target server hostname (overrides environments.<env>.server)",
)
@click.option(
    "--sudo",
    is_flag=True,
    help="Prefix remote commands with sudo (for non-root SSH users)",
)
@click.option(
    "--become-password-command",
    default=None,
    help='Shell command that prints the sudo password (e.g. "op read op://…")',
)
@click.option(
    "--ask-become-pass",
    "-K",
    is_flag=True,
    help="Prompt for sudo password (implies --sudo)",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def validate_remote(
    ctx: click.Context,
    fraise: str,
    environment: str,
    ssh_user: str | None,
    ssh_port: int | None,
    ssh_key: str | None,
    server: str | None,
    sudo: bool,
    become_password_command: str | None,
    ask_become_pass: bool,
    as_json: bool,
) -> None:
    """Check a server is ready to receive a deployment, without deploying.

    SSHes into the target server and runs read-only checks: SSH connectivity,
    bare git repo existence and ownership, app path ownership, systemd service
    and socket units, wrapper scripts, sudoers fragment, health endpoint, and
    the fraisier-webhook service.

    Use this before the first deploy to a new server, or to diagnose why a
    deploy is failing without having to trigger one.

    \b
    Examples:
        fraisier validate-remote my_api production
        fraisier validate-remote my_api staging --json
        fraisier validate-remote my_api production --ssh-user lionel
        fraisier validate-remote my_api production --ssh-user lionel --sudo
        fraisier validate-remote my_api production --ssh-user lionel -K
        fraisier validate-remote my_api production --become-password-command "op read op://…"
        fraisier validate-remote my_api production --server myserver.com
    """
    from fraisier.bootstrap import resolve_become_password
    from fraisier.remote_validator import RemoteDeploymentValidator

    config = require_config(ctx)

    fraise_config = config.get_fraise_environment(fraise, environment)
    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    # Resolution order (mirrors bootstrap):
    # 1. CLI --become-password-command
    # 2. bootstrap.environments.<env>.become_password_command
    # 3. bootstrap.servers.<server>.become_password_command
    # 4. bootstrap.become_password_command (global)
    sudo_password = None
    if become_password_command is None:
        raw_bootstrap = config._config.get("bootstrap", {}) or {}

        env_override = (raw_bootstrap.get("environments") or {}).get(environment) or {}
        become_password_command = env_override.get("become_password_command")

        if become_password_command is None:
            env_cfg = config.environments.get(environment)
            server_name = env_cfg.get("server") if isinstance(env_cfg, dict) else None
            if server_name:
                servers = raw_bootstrap.get("servers") or {}
                srv_override = servers.get(server_name) or {}
                become_password_command = srv_override.get("become_password_command")

        if become_password_command is None:
            become_password_command = raw_bootstrap.get("become_password_command")

    if become_password_command:
        sudo = True
        sudo_password = resolve_become_password(become_password_command)
    elif ask_become_pass:
        sudo = True
        sudo_password = click.prompt("SUDO password", hide_input=True, err=True)

    target_server, runner = _resolve_server_and_runner(
        ctx, environment, ssh_user, ssh_port, ssh_key, server,
        sudo=sudo, sudo_password=sudo_password,
    )

    if not as_json:
        console.print(
            f"\n[bold]Remote Validation: {fraise} / {environment}[/bold]"
            f" on [cyan]{target_server}[/cyan]\n"
        )

    validator = RemoteDeploymentValidator(fraise_config, runner, config)
    results = validator.run_all()

    all_passed = all(r.passed for r in results if r.severity == "error")
    has_errors = any(not r.passed and r.severity == "error" for r in results)

    if as_json:
        data = {
            "passed": all_passed,
            "server": target_server,
            "fraise": fraise,
            "environment": environment,
            "checks": [r.to_dict() for r in results],
        }
        click.echo(json.dumps(data, indent=2))
    else:
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
            table.add_row("⚠", r.name, f"[yellow]{r.message or 'warning'}[/yellow]")
        for r in errors:
            table.add_row("✗", r.name, f"[red]{r.message or 'failed'}[/red]")

        console.print(table)

        summary_parts = []
        if errors:
            summary_parts.append(f"[red]{len(errors)} failed[/red]")
        if warnings:
            summary_parts.append(f"[yellow]{len(warnings)} warning[/yellow]")
        if passed:
            summary_parts.append(f"[green]{len(passed)} passed[/green]")

        status_color = "green" if not has_errors else "red"
        status_text = "READY" if not has_errors else "NOT READY"
        console.print(
            f"\nSummary: {', '.join(summary_parts)} → "
            f"[{status_color}]{status_text}[/{status_color}]\n"
        )

    raise SystemExit(0 if all_passed else 1)

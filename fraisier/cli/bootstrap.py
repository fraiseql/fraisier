"""Bootstrap commands — provision and preflight-check a virgin server."""

from __future__ import annotations

from pathlib import Path

import click

from ._helpers import console, require_config
from .main import main


def _resolve_server_and_runner(
    ctx: click.Context,
    environment: str,
    ssh_user: str | None,
    ssh_port: int | None,
    ssh_key: str | None,
    server: str | None,
    sudo: bool = False,
    sudo_password: str | None = None,
) -> tuple[str, object]:
    """Resolve the target server and build an SSHRunner.

    Shared by ``bootstrap`` and ``bootstrap-preflight``.
    Returns ``(server_hostname, SSHRunner)``.
    """
    from fraisier.runners import SSHRunner
    from fraisier.ssh_config import resolve_ssh_config

    config = require_config(ctx)

    if server is None:
        env_cfg = config.environments.get(environment)
        if isinstance(env_cfg, dict):
            server = env_cfg.get("server")

    if not server:
        raise click.UsageError(
            f"environments.{environment}.server is not set in fraises.yaml.\n"
            f"Bootstrap requires a target host. Add it or use --server <host>."
        )

    host_config = resolve_ssh_config(server)
    resolved_user = ssh_user or host_config.user or "root"
    resolved_port = ssh_port if ssh_port is not None else (host_config.port or 22)
    resolved_key = str(ssh_key) if ssh_key else host_config.identity_file

    runner = SSHRunner(
        host=server,
        user=resolved_user,
        port=resolved_port,
        key_path=resolved_key,
        use_sudo=sudo,
        sudo_password=sudo_password,
    )
    return server, runner


# Common Click options shared between bootstrap commands
_ssh_user_option = click.option(
    "--ssh-user",
    default=None,
    help="SSH user for initial connection (default: ~/.ssh/config or root)",
)
_ssh_port_option = click.option(
    "--ssh-port",
    default=None,
    type=int,
    help="SSH port (default: from ~/.ssh/config or 22)",
)
_ssh_key_option = click.option(
    "--ssh-key",
    default=None,
    type=click.Path(),
    help="Path to SSH private key (default: from ~/.ssh/config)",
)
_server_option = click.option(
    "--server",
    default=None,
    help="Target server hostname (overrides environments.<env>.server)",
)
_environment_option = click.option(
    "--environment", "-e", required=True, help="Environment to bootstrap"
)


@main.command("bootstrap")
@_environment_option
@_ssh_user_option
@_ssh_port_option
@_ssh_key_option
@_server_option
@click.option("--dry-run", is_flag=True, help="Print steps without executing anything")
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
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose output")
@click.pass_context
def bootstrap(
    ctx: click.Context,
    environment: str,
    ssh_user: str | None,
    ssh_port: int | None,
    ssh_key: str | None,
    server: str | None,
    dry_run: bool,
    sudo: bool,
    become_password_command: str | None,
    ask_become_pass: bool,
    yes: bool,
    verbose: bool,
) -> None:
    """Provision a virgin server end-to-end via SSH.

    Connects as root (or --ssh-user) and runs 10 ordered, idempotent steps
    that bring a fresh server to a state where fraisier validate-setup passes
    and the first fraisier trigger-deploy can succeed.

    \b
    Examples:
        fraisier bootstrap --environment production
        fraisier bootstrap --environment staging --dry-run
        fraisier bootstrap --environment production --server myserver.com
        fraisier bootstrap -e production --ssh-user deployer --ssh-key ~/.ssh/id_ed25519
        fraisier bootstrap -e production --ssh-user lionel --sudo
        fraisier bootstrap -e production --ssh-user lionel -K
        fraisier bootstrap -e production --become-password-command "op read op://…"
    """
    from fraisier.bootstrap import ServerBootstrapper, resolve_become_password

    config = require_config(ctx)

    # Resolution order: CLI command > config command > interactive prompt
    sudo_password = None
    if become_password_command is None:
        raw_bootstrap = config._config.get("bootstrap", {}) or {}
        become_password_command = raw_bootstrap.get("become_password_command")

    if become_password_command:
        sudo = True
        sudo_password = resolve_become_password(become_password_command)
    elif ask_become_pass:
        sudo = True
        sudo_password = click.prompt("SUDO password", hide_input=True, err=True)
    server, runner = _resolve_server_and_runner(
        ctx,
        environment,
        ssh_user,
        ssh_port,
        ssh_key,
        server,
        sudo=sudo,
        sudo_password=sudo_password,
    )

    console.print(
        f"[cyan]Bootstrapping[/cyan] [bold]{environment}[/bold]"
        f" on [bold]{server}[/bold]..."
    )
    if dry_run:
        console.print("[yellow](DRY RUN — no changes will be made)[/yellow]")

    if not dry_run and not yes and not click.confirm("Continue?"):
        console.print("[yellow]Aborted.[/yellow]")
        return

    bootstrapper = ServerBootstrapper(
        config=config,
        environment=environment,
        runner=runner,
        fraises_yaml_path=Path(config.config_path),
        dry_run=dry_run,
        verbose=verbose,
    )

    result = bootstrapper.bootstrap()
    total = len(result.steps)

    for i, step in enumerate(result.steps, 1):
        color = "green" if step.success else "red"
        symbol = "✓" if step.success else "✗"
        already = " (already done)" if step.already_done and verbose else ""
        console.print(
            f"  [{color}][{i}/{total}] {step.name} ... {symbol}[/{color}]{already}"
        )
        if not step.success:
            if step.command:
                console.print(f"        Command: {step.command}")
            if step.error:
                console.print(f"        Error: {step.error}")
            console.print(
                "\n[red]Aborting. Fix the error above and re-run bootstrap.[/red]"
            )
            raise SystemExit(1)

    console.print(
        f"\n[green]Bootstrap complete.[/green] Server is ready for first deploy:\n"
        f"  fraisier trigger-deploy <fraise> {environment}"
    )


@main.command("bootstrap-preflight")
@_environment_option
@_ssh_user_option
@_ssh_port_option
@_ssh_key_option
@_server_option
@click.pass_context
def bootstrap_preflight(
    ctx: click.Context,
    environment: str,
    ssh_user: str | None,
    ssh_port: int | None,
    ssh_key: str | None,
    server: str | None,
) -> None:
    """Check server prerequisites before running bootstrap.

    Runs read-only SSH checks and reports what needs to be fixed.
    Does not make any changes to the server.

    \b
    Examples:
        fraisier bootstrap-preflight --environment production
        fraisier bootstrap-preflight -e staging --server myserver.com
        fraisier bootstrap-preflight -e production --ssh-user lionel
    """
    from fraisier.preflight import PreflightChecker

    config = require_config(ctx)
    server, runner = _resolve_server_and_runner(
        ctx, environment, ssh_user, ssh_port, ssh_key, server
    )

    console.print(
        f"[cyan]Checking[/cyan] [bold]{environment}[/bold] on [bold]{server}[/bold]..."
    )

    checker = PreflightChecker(
        runner=runner,
        deploy_user=config.scaffold.deploy_user,
    )
    result = checker.run_all()

    for check in result.checks:
        color = "green" if check.passed else "red"
        symbol = "✓" if check.passed else "✗"
        parts = [f"  [{color}]{symbol} {check.name}[/{color}]"]
        if check.message:
            parts.append(f" ({check.message})")
        if not check.passed and check.fix_hint:
            parts.append(f" — run: {check.fix_hint}")
        console.print("".join(parts))

    if result.passed:
        console.print(
            f"\n[green]All checks passed.[/green] Ready to bootstrap:\n"
            f"  fraisier bootstrap --environment {environment}"
        )
    else:
        n = result.failed_count
        console.print(
            f"\n[red]Fix {n} issue{'s' if n != 1 else ''} above, then run:[/red]\n"
            f"  fraisier bootstrap --environment {environment}"
        )
        raise SystemExit(1)

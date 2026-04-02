"""Bootstrap command — provision a virgin server end-to-end."""

from __future__ import annotations

from pathlib import Path

import click

from ._helpers import console, require_config
from .main import main


@main.command("bootstrap")
@click.option("--environment", "-e", required=True, help="Environment to bootstrap")
@click.option(
    "--ssh-user",
    default="root",
    show_default=True,
    help="Privileged SSH user for the initial connection",
)
@click.option(
    "--ssh-key",
    default=None,
    type=click.Path(),
    help="Path to SSH private key",
)
@click.option(
    "--server",
    default=None,
    help="Target server hostname (overrides environments.<env>.server in fraises.yaml)",
)
@click.option("--dry-run", is_flag=True, help="Print steps without executing anything")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose output")
@click.pass_context
def bootstrap(
    ctx: click.Context,
    environment: str,
    ssh_user: str,
    ssh_key: str | None,
    server: str | None,
    dry_run: bool,
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
    """
    from fraisier.bootstrap import ServerBootstrapper
    from fraisier.runners import SSHRunner

    config = require_config(ctx)

    # Resolve target server
    if server is None:
        env_cfg = config.environments.get(environment)
        if isinstance(env_cfg, dict):
            server = env_cfg.get("server")

    if not server:
        raise click.UsageError(
            f"environments.{environment}.server is not set in fraises.yaml.\n"
            f"Bootstrap requires a target host. Add it or use --server <host>."
        )

    runner = SSHRunner(
        host=server,
        user=ssh_user,
        key_path=str(ssh_key) if ssh_key else None,
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

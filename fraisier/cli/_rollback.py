"""Rollback CLI command."""

from __future__ import annotations

import click

from ._helpers import _get_deployer, console
from .main import main


@main.command()
@click.argument("fraise")
@click.argument("environment")
@click.option("--to-version", default=None, help="Target SHA to roll back to")
@click.option("--dry-run", is_flag=True, help="Show rollback plan without executing")
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def rollback(
    ctx: click.Context,
    fraise: str,
    environment: str,
    to_version: str | None,
    dry_run: bool,
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
    current = deployer.get_current_version()

    if not target:
        from fraisier.database import get_db

        db = get_db()
        history = db.get_recent_deployments(
            limit=20, fraise=fraise, environment=environment
        )
        successful = [d for d in history if d["status"] == "success"]

        # Find the most recent successful deployment that is not the current version
        target_deployment = None
        for deployment in successful:
            if deployment.get("new_version") != current:
                target = deployment.get("new_version")
                target_deployment = deployment
                break

    if not target:
        console.print("[red]No previous version found to roll back to[/red]")
        console.print("Use --to-version <sha> to specify a target explicitly.")
        raise SystemExit(1)

    # Safety check: ensure target is not too far back in history
    if target_deployment and not force:
        target_index = None
        for i, deployment in enumerate(history):
            if deployment["id"] == target_deployment["id"]:
                target_index = i
                break

        if (
            target_index is not None and target_index >= 10
        ):  # 0-indexed, so >= 10 means 11th or later
            console.print(
                f"[red]Safety limit exceeded:[/red] Target deployment is "
                f"{target_index + 1} deployments back."
            )
            console.print("Use --force to override this safety check.")
            raise SystemExit(1)

    # Show rollback plan
    console.print(f"Will roll back [bold]{fraise}[/bold] ({environment})")
    console.print(f"  From: {current or 'unknown'}")
    console.print(f"  To:   {target[:8]}")

    if dry_run:
        console.print("[yellow]DRY RUN[/yellow] - No changes will be made")
        return

    if not force:
        if not click.confirm("Proceed?"):
            console.print("Aborted.")
            return

    result = deployer.rollback(to_version=target)

    if result.success:
        console.print(f"[green]Rolled back {fraise} to {result.new_version}[/green]")
    else:
        console.print(f"[red]Rollback failed: {result.error_message}[/red]")
        raise SystemExit(1)

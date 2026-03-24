"""Database management commands (db group, backup, db-check)."""

from __future__ import annotations

import asyncio

import click
from rich.table import Table

from ._helpers import console
from .main import main


@main.group()
@click.pass_context
def db(ctx: click.Context) -> None:
    """Database management commands.

    \b
    Examples:
        fraisier db reset management -e development
        fraisier db migrate management -e production
        fraisier db build management -e development
    """


def _get_db_config(
    config, fraise_name: str, environment: str
) -> tuple[dict | None, dict | None]:
    """Get fraise config and its database section."""
    fraise = config.get_fraise(fraise_name)
    if not fraise:
        return None, None
    env_config = config.get_fraise_environment(fraise_name, environment)
    if not env_config:
        return fraise, None
    return fraise, env_config


@db.command(name="reset")
@click.argument("fraise")
@click.option("--env", "-e", required=True, help="Target environment")
@click.option("--force", is_flag=True, help="Reset even without template")
@click.pass_context
def db_reset(
    ctx: click.Context,
    fraise: str,
    env: str,
    force: bool,  # noqa: ARG001
) -> None:
    """Reset database from template (sub-second).

    \b
    Examples:
        fraisier db reset management -e development
        fraisier db reset management -e development --force
    """
    from fraisier.dbops.guard import is_external_db
    from fraisier.dbops.templates import reset_from_template

    config = ctx.obj["config"]
    fraise_cfg, env_config = _get_db_config(config, fraise, env)

    if not fraise_cfg or not env_config:
        console.print(f"[red]Error:[/red] Fraise '{fraise}' env '{env}' not found")
        raise SystemExit(1)

    if is_external_db(fraise_cfg):
        console.print(f"[yellow]Skipping '{fraise}': external_db is true[/yellow]")
        return

    db_cfg = env_config.get("database", {})
    db_name = db_cfg.get("name", fraise)
    prefix = db_cfg.get("template_prefix", "template_")

    result = reset_from_template(db_name, prefix=prefix)

    if result.success:
        console.print(f"[green]Reset '{db_name}' from {result.template_name}[/green]")
    else:
        console.print(f"[red]Reset failed:[/red] {result.error}")
        raise SystemExit(1)


@db.command(name="migrate")
@click.argument("fraise")
@click.option("--env", "-e", required=True, help="Target environment")
@click.option(
    "--direction",
    "-d",
    default="up",
    type=click.Choice(["up", "down"]),
    help="Migration direction",
)
@click.pass_context
def db_migrate(ctx: click.Context, fraise: str, env: str, direction: str) -> None:
    """Run database migrations.

    \b
    Examples:
        fraisier db migrate management -e production
        fraisier db migrate management -e production -d down
    """
    from fraisier.dbops.confiture import confiture_migrate
    from fraisier.dbops.guard import is_external_db

    config = ctx.obj["config"]
    fraise_cfg, env_config = _get_db_config(config, fraise, env)

    if not fraise_cfg or not env_config:
        console.print(f"[red]Error:[/red] Fraise '{fraise}' env '{env}' not found")
        raise SystemExit(1)

    if is_external_db(fraise_cfg):
        console.print(f"[yellow]Skipping '{fraise}': external_db is true[/yellow]")
        return

    db_cfg = env_config.get("database", {})
    confiture_config = db_cfg.get("confiture_config", "confiture.yaml")
    app_path = env_config.get("app_path", ".")

    result = confiture_migrate(
        config_path=confiture_config,
        cwd=app_path,
        direction=direction,
    )

    if result.success:
        console.print(
            f"[green]Migration {direction}: {result.migration_count} applied[/green]"
        )
    else:
        console.print(f"[red]Migration failed:[/red] {result.error}")
        raise SystemExit(1)


@db.command(name="build")
@click.argument("fraise")
@click.option("--env", "-e", required=True, help="Target environment")
@click.option("--rebuild", is_flag=True, help="Drop and rebuild")
@click.pass_context
def db_build(ctx: click.Context, fraise: str, env: str, rebuild: bool) -> None:
    """Build database schema (dev/test environments).

    \b
    Examples:
        fraisier db build management -e development
        fraisier db build management -e development --rebuild
    """
    from fraisier.dbops.confiture import confiture_build
    from fraisier.dbops.guard import is_external_db

    config = ctx.obj["config"]
    fraise_cfg, env_config = _get_db_config(config, fraise, env)

    if not fraise_cfg or not env_config:
        console.print(f"[red]Error:[/red] Fraise '{fraise}' env '{env}' not found")
        raise SystemExit(1)

    if is_external_db(fraise_cfg):
        console.print(f"[yellow]Skipping '{fraise}': external_db is true[/yellow]")
        return

    db_cfg = env_config.get("database", {})
    confiture_config = db_cfg.get("confiture_config", "confiture.yaml")
    app_path = env_config.get("app_path", ".")

    result = confiture_build(
        config_path=confiture_config,
        cwd=app_path,
        rebuild=rebuild,
    )

    if result.success:
        console.print(
            f"[green]Build complete: {result.migration_count} migrations[/green]"
        )
    else:
        console.print(f"[red]Build failed:[/red] {result.error}")
        raise SystemExit(1)


@main.command(name="backup")
@click.argument("fraise")
@click.option("--env", "-e", required=True, help="Target environment")
@click.option(
    "--mode",
    "-m",
    default="full",
    type=click.Choice(["full", "slim"]),
    help="Backup mode",
)
@click.pass_context
def backup_cmd(ctx: click.Context, fraise: str, env: str, mode: str) -> None:
    """Run database backup for a fraise.

    \b
    Examples:
        fraisier backup management -e production
        fraisier backup management -e production --mode slim
    """
    from fraisier.dbops.backup import check_disk_space, run_backup
    from fraisier.dbops.guard import is_external_db

    config = ctx.obj["config"]
    fraise_cfg, env_config = _get_db_config(config, fraise, env)

    if not fraise_cfg or not env_config:
        console.print(f"[red]Error:[/red] Fraise '{fraise}' env '{env}' not found")
        raise SystemExit(1)

    if is_external_db(fraise_cfg):
        console.print(f"[yellow]Skipping '{fraise}': external_db is true[/yellow]")
        return

    # Get backup config from top-level or fraise-level
    backup_cfg = config._config.get("backup", {}) or {}
    db_cfg = env_config.get("database", {})
    db_name = db_cfg.get("name", fraise)

    compression = backup_cfg.get("compression", "zstd:9")
    required_gb = backup_cfg.get("disk_space_required_gb", 2)

    # Find output dir from first destination or default
    destinations = backup_cfg.get("destinations", [])
    output_dir = "/backup"
    if destinations:
        output_dir = destinations[0].get("path", output_dir)

    # Check disk space
    if not check_disk_space(output_dir, required_gb=required_gb):
        console.print(
            f"[red]Error:[/red] Insufficient disk space "
            f"(need {required_gb}GB at {output_dir})"
        )
        raise SystemExit(1)

    # Get excluded tables for slim mode
    excluded_tables: list[str] = []
    if mode == "slim":
        slim_cfg = backup_cfg.get("slim", {})
        excluded_tables = slim_cfg.get("excluded_tables", [])

    result = run_backup(
        db_name=db_name,
        output_dir=output_dir,
        compression=compression,
        mode=mode,
        excluded_tables=excluded_tables,
    )

    if result.success:
        console.print(f"[green]Backup saved: {result.backup_path}[/green]")
    else:
        console.print(f"[red]Backup failed:[/red] {result.error}")
        raise SystemExit(1)


@main.command(name="db-check")
@click.pass_context
def db_check(_ctx: click.Context) -> None:
    """Check database health and show connection pool metrics.

    Verifies database connectivity and displays:
    - Database type and version
    - Connection pool status
    - Query performance
    - Recent errors

    \b
    Examples:
        fraisier db-check
    """
    from fraisier.db.factory import get_database_adapter

    async def _check_db():
        try:
            adapter = await get_database_adapter()
            await adapter.connect()

            try:
                # Test connectivity
                console.print("[cyan]Testing database connectivity...[/cyan]")
                await adapter.execute_query("SELECT 1")
                console.print("[green]\u2713 Database connection successful[/green]")

                # Get pool metrics
                metrics = adapter.pool_metrics()
                console.print("\n[bold]Connection Pool Status:[/bold]")
                pool_table = Table(show_header=True, header_style="bold cyan")
                pool_table.add_column("Metric", style="dim")
                pool_table.add_column("Value")
                pool_table.add_row(
                    "Active connections", str(metrics.active_connections)
                )
                pool_table.add_row("Idle connections", str(metrics.idle_connections))
                pool_table.add_row(
                    "Total connections",
                    str(metrics.active_connections + metrics.idle_connections),
                )
                pool_table.add_row("Waiting requests", str(metrics.waiting_requests))
                console.print(pool_table)

                # Get database info
                console.print("\n[bold]Database Information:[/bold]")
                db_type = adapter.database_type()
                info_table = Table(show_header=False)
                info_table.add_row("[dim]Type:[/dim]", str(db_type.value).upper())
                console.print(info_table)

                console.print("\n[green]\u2713 All database checks passed[/green]")

            finally:
                await adapter.disconnect()

        except Exception as e:
            console.print(f"[red]\u2717 Database health check failed:[/red] {e}")
            raise SystemExit(1) from e

    try:
        asyncio.run(_check_db())
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1) from e

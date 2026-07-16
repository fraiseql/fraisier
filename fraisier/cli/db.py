"""Database management commands (db group, backup, db-check)."""

from __future__ import annotations

import asyncio
from pathlib import Path

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


@db.command(name="exec")
@click.argument("fraise")
@click.option("--env", "-e", required=True, help="Target environment")
@click.argument("sql", required=False)
@click.option(
    "--file",
    "-f",
    "sql_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Read SQL from a file",
)
@click.option(
    "--json",
    "output_format",
    flag_value="json",
    default=False,
    help="Output as JSON",
)
@click.option(
    "--csv",
    "output_format",
    flag_value="csv",
    default=False,
    help="Output as CSV",
)
@click.option(
    "--timeout",
    default=30,
    show_default=True,
    help="Statement timeout in seconds",
)
@click.pass_context
def db_exec(
    ctx: click.Context,
    fraise: str,
    env: str,
    sql: str | None,
    sql_file: str | None,
    output_format: bool,
    timeout: int,
) -> None:
    """Execute a read-only SQL statement on a remote or local database.

    Connects using the database.admin_url from fraises.yaml.
    Statements are always run with statement_timeout applied.
    Only SELECT, EXPLAIN, SHOW, WITH, and TABLE are permitted.

    \b
    Examples:
        fraisier db exec api -e production "SELECT count(*) FROM tb_user"
        fraisier db exec api -e staging "EXPLAIN ANALYZE SELECT * FROM v_reading"
        fraisier db exec api -e staging --csv "SELECT id FROM tb_org LIMIT 10"
        fraisier db exec api -e staging --file query.sql
    """
    import subprocess

    from fraisier import ssh
    from fraisier.dbops.exec import build_psql_argv, is_readonly_sql

    if sql and sql_file:
        console.print("[red]Error:[/red] SQL arg and --file are mutually exclusive")
        raise SystemExit(1)
    if not sql and not sql_file:
        console.print("[red]Error:[/red] Provide SQL or use --file")
        raise SystemExit(1)

    if sql_file:
        sql = Path(sql_file).read_text()

    assert sql is not None  # ensured by validation above

    config = ctx.obj["config"]
    fraise_cfg, env_config = _get_db_config(config, fraise, env)
    if not fraise_cfg or not env_config:
        console.print(f"[red]Error:[/red] Fraise '{fraise}' env '{env}' not found")
        raise SystemExit(1)

    db_cfg = env_config.get("database")
    if not db_cfg:
        console.print(
            f"[red]Error:[/red] No database config for '{fraise}' env '{env}'"
        )
        raise SystemExit(1)

    db_target = db_cfg.get("admin_url") or db_cfg.get("name") or fraise

    if output_format == "json":
        actual_format = "json"
    elif output_format == "csv":
        actual_format = "csv"
    else:
        actual_format = "table"

    if not is_readonly_sql(sql):
        console.print(
            "[red]Error:[/red] Only read-only statements are permitted "
            "(SELECT, EXPLAIN, SHOW, WITH, TABLE).\n"
            "Write access is not supported by this command."
        )
        raise SystemExit(1)

    if timeout <= 0:
        console.print("[red]Error:[/red] --timeout must be a positive integer")
        raise SystemExit(1)

    if env.lower() == "production":
        console.print(
            "[yellow]Warning:[/yellow] You are about to run SQL on production."
        )
        click.confirm("Continue?", abort=True)

    timeout_ms = timeout * 1000
    argv = build_psql_argv(
        db_target, sql, timeout_ms=timeout_ms, output_format=actual_format
    )

    ssh_config = env_config.get("ssh")
    if ssh_config:
        target = ssh.SshTarget.from_config(ssh_config)
        try:
            proc = ssh.short_cmd(target, argv, timeout=timeout + 10)
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr or str(exc)
            console.print(f"[red]Error:[/red] {stderr}")
            host = ssh_config.get("host", "<host>")
            console.print(
                f"[yellow]Hint:[/yellow] Check SSH connectivity: ssh {host} echo ok"
            )
            raise SystemExit(1) from exc
        console.print(proc.stdout, end="")
    else:
        proc = subprocess.run(argv, check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            console.print(f"[red]Error:[/red] {proc.stderr or proc.stdout}")
            raise SystemExit(proc.returncode)
        console.print(proc.stdout, end="")


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
    admin_url = db_cfg.get("admin_url")
    if not admin_url:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' env '{env}' has no admin_url; "
            "set database.admin_url in fraise/env/*.yaml"
        )
        raise SystemExit(1)

    result = reset_from_template(db_name, prefix=prefix, connection_url=admin_url)

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


@db.command(name="preflight")
@click.argument("fraise")
@click.option("--env", "-e", required=True, help="Target environment")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format",
)
@click.pass_context
def db_preflight(ctx: click.Context, fraise: str, env: str, fmt: str) -> None:
    """Test pending migrations against a schema-only copy of the backup.

    Creates a temporary database, extracts the schema from the configured
    backup, runs all pending migrations via SAVEPOINT (always rolled back),
    and reports results. The original database is never touched.

    \b
    Examples:
        fraisier db preflight myapp -e staging
        fraisier db preflight myapp -e staging --format json
    """
    import json as _json
    from pathlib import Path as _Path

    from fraisier.dbops.preflight import (
        MigrationPreflightResult,
        run_migration_preflight,
    )
    from fraisier.dbops.restore import find_latest_backup

    config = ctx.obj["config"]
    fraise_cfg, env_config = _get_db_config(config, fraise, env)

    if not fraise_cfg or not env_config:
        console.print(f"[red]Error:[/red] Fraise '{fraise}' env '{env}' not found")
        raise SystemExit(1)

    db_cfg = env_config.get("database", {})
    restore_cfg = db_cfg.get("restore")

    if not restore_cfg:
        console.print(
            f"[red]Error:[/red] No 'restore' config for '{fraise}' env '{env}'. "
            "Migration preflight requires a restore strategy with backup config."
        )
        raise SystemExit(2)

    admin_url = db_cfg.get("admin_url")
    if not admin_url:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' env '{env}' has no admin_url"
        )
        raise SystemExit(1)

    app_path = _Path(env_config.get("app_path", "."))
    confiture_config_rel = _Path(db_cfg.get("confiture_config", "confiture.yaml"))
    confiture_config = (
        confiture_config_rel
        if confiture_config_rel.is_absolute()
        else app_path / confiture_config_rel
    )
    migrations_dir = app_path / "db" / "migrations"

    backup_dir = _Path(restore_cfg["backup_dir"])
    backup_pattern = restore_cfg.get("backup_pattern", "*.dump")
    backup_file = find_latest_backup(backup_dir, pattern=backup_pattern)

    if backup_file is None:
        console.print(
            f"[red]Error:[/red] No backup matching '{backup_pattern}' in {backup_dir}"
        )
        raise SystemExit(1)

    result: MigrationPreflightResult = run_migration_preflight(
        backup_path=backup_file,
        admin_url=admin_url,
        confiture_config=confiture_config,
        migrations_dir=migrations_dir,
    )

    if fmt == "json":
        print(
            _json.dumps(
                {
                    "all_passed": result.all_passed,
                    "total_ms": result.total_ms,
                    "schema_extraction_ms": result.schema_extraction_ms,
                    "migration_count": len(result.migrations),
                    "failure_count": result.failure_count,
                    "suspected_false_positive_count": len(
                        result.suspected_false_positive_failures
                    ),
                    "migrations": [
                        {
                            "version": m.version,
                            "name": m.name,
                            "passed": m.passed,
                            "error": m.error,
                            "time_ms": m.time_ms,
                            "skipped": m.skipped,
                        }
                        for m in result.migrations
                    ],
                }
            )
        )
    else:
        console.print(
            f"\nMigration preflight: {len(result.migrations)} migration(s) checked "
            f"(schema extracted in {result.schema_extraction_ms}ms)\n"
        )
        for m in result.migrations:
            if m.skipped:
                console.print(f"  -  {m.version}  {m.name}  [skipped]", style="dim")
            elif m.passed:
                console.print(f"  +  {m.version}  {m.name}  ({m.time_ms}ms)")
            else:
                console.print(f"  !  {m.version}  {m.name}", style="red")
                if m.error:
                    console.print(f"       Error: {m.error}", style="dim")
        console.print()
        if result.all_passed:
            console.print(
                f"  Preflight passed: all {len(result.migrations)} migration(s) OK "
                f"({result.total_ms}ms)",
                style="green",
            )
        else:
            console.print(
                f"  Preflight failed: {result.failure_count} of "
                f"{len(result.migrations)} migration(s) would fail.",
                style="red bold",
            )
            note = result.false_positive_note
            if note:
                console.print(f"\n  Note: {note}", style="yellow")
            else:
                console.print(
                    "\n  To bypass for an emergency restore: "
                    "`fraisier db restore <fraise> <env> --skip-preflight`",
                    style="dim",
                )
        console.print("  [Rolled back — preflight DB dropped]\n", style="dim")

    raise SystemExit(0 if result.all_passed else 1)


@db.command(name="restore")
@click.argument("fraise")
@click.argument("environment")
@click.option(
    "--from-backup",
    "from_backup",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Explicit backup file (skips latest-file discovery and age check)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would happen without executing",
)
@click.option(
    "--no-service-restart",
    is_flag=True,
    help="Skip stopping/restarting the systemd service",
)
@click.option(
    "--skip-preflight",
    is_flag=True,
    help="Skip migration preflight check (emergency restores only)",
)
@click.option(
    "--jobs",
    type=int,
    default=None,
    help="Number of parallel pg_restore jobs (overrides config restore.jobs)",
)
@click.option(
    "--preferred-compression",
    "preferred_compression",
    type=click.Choice(["zstd", "lz4", "gzip", "none"]),
    default=None,
    help="Prefer backups compressed with this algorithm (overrides config)",
)
@click.pass_context
def db_restore(
    ctx: click.Context,
    fraise: str,
    environment: str,
    from_backup: Path | None,
    dry_run: bool,
    no_service_restart: bool,
    skip_preflight: bool,
    jobs: int | None,
    preferred_compression: str | None,
) -> None:
    """Restore staging database from a production backup.

    Stops the service, runs pg_restore, creates a rollback template,
    applies pending migrations, and restarts the service.

    \b
    Examples:
        fraisier db restore api staging
        fraisier db restore api staging --from-backup /backup/production/latest.dump
        fraisier db restore api staging --dry-run
    """
    from pathlib import Path as _Path

    from fraisier.dbops.guard import is_external_db
    from fraisier.dbops.restore import find_latest_backup, validate_backup_age
    from fraisier.errors import DatabaseError
    from fraisier.runners import LocalRunner
    from fraisier.service_managers import get_service_manager
    from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy

    config = ctx.obj["config"]
    fraise_cfg, env_config = _get_db_config(config, fraise, environment)

    if not fraise_cfg or not env_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    if is_external_db(fraise_cfg):
        console.print(f"[yellow]Skipping '{fraise}': external_db is true[/yellow]")
        return

    db_cfg = env_config.get("database", {})
    restore_cfg = db_cfg.get("restore")

    if not restore_cfg:
        console.print(
            f"[red]Error:[/red] No 'restore' config for '{fraise}' environment "
            f"'{environment}'"
        )
        raise SystemExit(1)

    db_name = db_cfg.get("name", fraise)
    app_path = _Path(env_config.get("app_path", "."))
    confiture_config_rel = _Path(db_cfg.get("confiture_config", "confiture.yaml"))
    confiture_config = (
        confiture_config_rel
        if confiture_config_rel.is_absolute()
        else app_path / confiture_config_rel
    )
    from fraisier.naming import resolve_systemd_service

    systemd_service: str | None = resolve_systemd_service(env_config)
    admin_url = db_cfg.get("admin_url")
    if not admin_url:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' env '{environment}' has no "
            "admin_url; set database.admin_url in fraise/env/*.yaml"
        )
        raise SystemExit(1)

    # --- dry-run: resolve backup and print plan, then exit ---
    if dry_run:
        if from_backup:
            backup_file = from_backup
            age_check = "skipped (explicit path)"
        else:
            backup_dir = _Path(restore_cfg["backup_dir"])
            pattern = restore_cfg.get("backup_pattern", "*.dump")
            backup_file = find_latest_backup(backup_dir, pattern=pattern)
            if backup_file is None:
                console.print(
                    f"[red]Error:[/red] No backup matching '{pattern}' in {backup_dir}"
                )
                raise SystemExit(1)
            max_age = float(restore_cfg.get("max_age_hours", 48.0))
            age_ok = validate_backup_age(backup_file, max_age_hours=max_age)
            age_check = "[green]OK[/green]" if age_ok else "[red]TOO OLD[/red]"

        console.print("[bold cyan]Dry-run restore plan:[/bold cyan]")
        console.print(f"  Backup:          {backup_file}  (age check: {age_check})")
        console.print(f"  Database:        {db_name}")
        console.print(f"  Migrations:      {confiture_config} (cwd: {app_path})")
        console.print(f"  Create template: {restore_cfg.get('create_template', False)}")
        console.print(f"  Min tables:      {restore_cfg.get('min_tables', 0)}")
        if systemd_service and not no_service_restart:
            svc_action = f"stop → restore → restart  ({systemd_service})"
            console.print(f"  Service:         {svc_action}")
        else:
            console.print("  Service:         not managed")
        console.print("\n[yellow]Dry-run complete. No changes made.[/yellow]")
        return

    # --- live run ---
    runner = LocalRunner()
    svc_mgr = (
        get_service_manager(runner, config._config)
        if systemd_service and not no_service_restart
        else None
    )

    from fraisier.config.schema import PreflightConfig

    preflight_cfg = db_cfg.get("preflight") or {}
    preflight = PreflightConfig(
        enabled=bool(preflight_cfg.get("enabled", True)),
        timeout_seconds=int(preflight_cfg.get("timeout_seconds", 120)),
    )

    strategy = RestoreMigrateStrategy(
        RestoreConfig(
            db_name=db_name,
            backup_dir=_Path(restore_cfg.get("backup_dir", ".")),
            backup_pattern=restore_cfg.get("backup_pattern", "*.dump"),
            max_age_hours=float(restore_cfg.get("max_age_hours", 48.0)),
            target_owner=restore_cfg.get("target_owner"),
            create_template=bool(restore_cfg.get("create_template", False)),
            template_name=restore_cfg.get("template_name"),
            min_tables=int(restore_cfg.get("min_tables", 0)),
            jobs=jobs if jobs is not None else int(restore_cfg.get("jobs", 1)),
            preferred_compression=(
                preferred_compression
                if preferred_compression is not None
                else restore_cfg.get("preferred_compression")
            ),
            backup_path=from_backup,
            preflight=preflight,
        ),
        admin_url=admin_url,
        service_manager=svc_mgr,
        service_name=systemd_service,
    )

    try:
        result = strategy.execute(
            confiture_config,
            migrations_dir=app_path / "db" / "migrations",
            skip_preflight=skip_preflight,
        )
    except DatabaseError as exc:
        if svc_mgr and systemd_service:
            msg = f"[yellow]Restarting {systemd_service} after error...[/yellow]"
            console.print(msg)
            svc_mgr.restart(systemd_service)
        console.print(f"[red]Restore failed:[/red] {exc}")
        raise SystemExit(1) from exc

    if not result.success:
        errors = ", ".join(result.errors)
        console.print(f"[red]Restore failed:[/red] {errors}")
        raise SystemExit(1)

    msg = f"{result.migrations_applied} migration(s) applied"
    console.print(f"[green]Restore complete:[/green] {msg}")
    if result.total_duration_seconds > 0:
        console.print(
            f"  Restore: {result.restore_duration_seconds:.1f}s"
            f" | Migration: {result.migration_duration_seconds:.1f}s"
            f" | Total: {result.total_duration_seconds:.1f}s"
        )

    # Re-apply configured grant scripts. The restore strategy restores with
    # pg_restore --no-owner --no-acl, so without this the restored DB is
    # grantless for every non-owner role — the deploy path already does this
    # via _run_post_migrate, the standalone CLI path did not (issue #273).
    from fraisier import post_migrate
    from fraisier.errors import DeploymentError

    try:
        post_migrate.run_configured_post_migrate(
            db_cfg,
            app_path=app_path,
            runner=runner,
        )
    except DeploymentError as exc:
        console.print(f"[red]post_migrate failed:[/red] {exc}")
        raise SystemExit(1) from exc


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
@click.option(
    "--jobs",
    type=int,
    default=None,
    help=(
        "Number of parallel pg_dump workers (overrides config backup.jobs). "
        "Values >1 switch to directory-format dumps."
    ),
)
@click.pass_context
def backup_cmd(
    ctx: click.Context, fraise: str, env: str, mode: str, jobs: int | None
) -> None:
    """Run database backup for a fraise.

    \b
    Examples:
        fraisier backup management -e production
        fraisier backup management -e production --mode slim
        fraisier backup management -e production --jobs 4
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
    database_url = db_cfg.get("database_url")
    if not database_url:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' env '{env}' has no "
            "database_url; set database.database_url in fraise/env/*.yaml"
        )
        raise SystemExit(1)

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

    effective_jobs = jobs if jobs is not None else int(backup_cfg.get("jobs", 1))

    result = run_backup(
        db_name=db_name,
        output_dir=output_dir,
        database_url=database_url,
        compression=compression,
        mode=mode,
        excluded_tables=excluded_tables,
        jobs=effective_jobs,
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
        fraisier db-check 2>&1 | tee db-health.log
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

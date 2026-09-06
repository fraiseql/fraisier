"""Database management commands (db group, backup, db-check)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import click
from rich.table import Table

from . import _json
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


def _report_actuation(actuation) -> None:
    """Print what proved — or did not prove — that this run rewrote the database.

    Three outcomes, three different lines, on purpose. The one failure this
    exists to catch is a restore that reports success while staging keeps
    yesterday's data (#343, #356), so a run that could not obtain the proof must
    not print anything a reader could mistake for having obtained it.

    ``None`` means the strategy predates the receipt — say nothing rather than
    invent a verdict for it.
    """
    from fraisier.dbops.receipt import ActuationVerdict

    if actuation is None:
        return
    if actuation.verdict is ActuationVerdict.ACTUATED and actuation.receipt:
        receipt = actuation.receipt
        console.print(
            f"  Actuation: run {receipt.run_id} wrote this database, read back "
            f"out of it — the restore ran, it did not merely report success."
        )
    elif actuation.verdict is ActuationVerdict.STALE:
        console.print(f"  [red]Actuation failed:[/red] {actuation.detail}")
    else:
        console.print(
            f"  [yellow]Actuation not verified:[/yellow] {actuation.detail}. "
            "This says nothing either way — it is not a passed check."
        )


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

    # Hold the same per-fraise lock the webhook takes, for the reason #310 gave
    # `db restore` (#389). This is the more destructive of the two:
    # `reset_from_template` force-disconnects every client, drops the database
    # and recreates it from its template. Run against a fraise mid-deploy it
    # takes the schema out from under a running migration.
    #
    # No `--skip-if-locked`: `db restore` has one because its generated timer
    # unit passes it, and a skipped nightly restore is a non-event. Nothing
    # schedules `db reset`, and a silently skipped reset would leave the
    # operator with the database they were trying to replace.
    from fraisier.errors import DeploymentLockError
    from fraisier.locking import deployment_lock

    try:
        with deployment_lock(fraise):
            result = reset_from_template(
                db_name, prefix=prefix, connection_url=admin_url
            )
    except DeploymentLockError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        console.print(
            "  A deploy is in progress for this fraise. Resetting now would "
            "drop the database it is migrating. Retry once it finishes."
        )
        raise SystemExit(1) from exc

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
@click.option(
    "--skip-if-locked",
    is_flag=True,
    help=(
        "Exit 0 instead of failing when a deploy holds the lock. "
        "For timer units, where a skipped nightly restore is a non-event."
    ),
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
    skip_if_locked: bool,
) -> None:
    """Restore staging database from a production backup.

    Stops the service, runs pg_restore, creates a rollback template,
    applies pending migrations, and restarts the service.

    Holds the same per-fraise deployment lock the webhook uses, so a restore
    and a deploy of the same fraise can never interleave. --dry-run does not
    take the lock, since it changes nothing.

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
    # Hold the same per-fraise lock the webhook takes (#310). A timer-, cron- or
    # hand-driven restore stops the service and terminates every connection to
    # the database; without this it can do that underneath an in-flight deploy
    # of the same fraise, killing its pg_restore and leaving the DB half-done.
    from fraisier.errors import DeploymentLockError
    from fraisier.locking import deployment_lock

    try:
        with deployment_lock(fraise):
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
                # This command runs from wherever the operator invoked it, not
                # from the app. Naming the project makes its migrate step
                # resolve relative paths the way the deploy path does (#371).
                project_dir=app_path,
            )

            try:
                result = strategy.execute(
                    confiture_config,
                    migrations_dir=app_path / "db" / "migrations",
                    skip_preflight=skip_preflight,
                )
            except DatabaseError as exc:
                if svc_mgr and systemd_service:
                    msg = (
                        f"[yellow]Restarting {systemd_service} after error...[/yellow]"
                    )
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

            # Said, not assumed (#343). `min_tables` defaulting to 0 meant no
            # table-count floor ran anywhere, while a comment in dbops.restore
            # claimed the strategy enforced one. An operator reading "Restore
            # complete" should learn whether anything counted. This goes to the
            # console rather than the log because `fraisier` only configures
            # logging under -v, and the generated timer unit passes no flags —
            # an INFO line would reach neither the terminal nor the journal.
            # The archive can state its own floor now (#343), so "not checked"
            # stopped being true whenever it does. What must not creep in is the
            # opposite claim: a table count proves the schema arrived and says
            # nothing about the data, which `dbops/restore.py` documents.
            schema_floor = getattr(result, "schema_floor", None)
            if schema_floor is not None:
                floor_schema, floor_tables = schema_floor
                console.print(
                    f"  Schema floor: {floor_tables} base table(s) required in "
                    f"'{floor_schema}', from the archive's own table of "
                    "contents — met."
                )
                unchecked = getattr(result, "unchecked_schemas", ())
                if unchecked:
                    console.print(
                        "  [yellow]Not checked:[/yellow] "
                        f"{', '.join(unchecked)} also carry table data in this "
                        "archive; the floor covers the largest schema only."
                    )
                console.print(
                    "  [dim]This checks the schema arrived, not the data — a "
                    "dump truncated inside its data section restores a "
                    "complete, empty schema.[/dim]"
                )
            elif int(restore_cfg.get("min_tables", 0)) <= 0:
                console.print(
                    "  [yellow]Note:[/yellow] no table-count floor configured "
                    "(database.restore.min_tables) and the archive stated none "
                    "(--schema-only, or pg_restore unavailable); the restored "
                    "database was not checked for emptiness."
                )

            # What the floor above cannot tell anyone: that a restore happened
            # at all (#358). A staging database nobody rewrote holds correct
            # counts of yesterday's data, so the evidence has to be a token this
            # run minted and then read back out of the database. Reported here
            # in the same voice as the floor — including when it could not be
            # obtained, because "not verified" and "verified" must not look
            # alike to someone skimming a nightly log.
            _report_actuation(getattr(result, "actuation", None))

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
    except DeploymentLockError as exc:
        if skip_if_locked:
            # Timer units pass this: a skipped nightly restore is a non-event,
            # since a concurrent staging deploy is itself restoring from prod.
            console.print(f"[yellow]Skipping restore:[/yellow] {exc}")
            return
        console.print(f"[red]Error:[/red] {exc}")
        console.print(
            "  A deploy is in progress for this fraise. Retry once it finishes, "
            "or pass --skip-if-locked (used by the generated timer unit)."
        )
        raise SystemExit(1) from exc
    except OSError as exc:
        # The lock could not be evaluated at all — /run/fraisier is tmpfs and is
        # created by the webhook unit's RuntimeDirectory=. Deliberately not
        # covered by --skip-if-locked: "I cannot tell whether a deploy is
        # running" must never be treated as "no deploy is running".
        console.print(f"[red]Error:[/red] cannot acquire the deployment lock: {exc}")
        console.print(
            "  The lock directory (default /run/fraisier) must exist and be "
            "writable by this user. It is created by the webhook unit and by "
            "`fraisier setup`; after a reboot it appears when the webhook starts."
        )
        raise SystemExit(1) from exc


#: ``db receipt`` exits 3 when it could not reach a verdict. Not 0: a host that
#: cannot check must not report what a host that checked and passed reports —
#: that is the ``min_tables=0`` silent hole (#343) moved into monitoring. Not 1
#: either, because a monitoring timer that pages on a stale staging database
#: should not page on a host missing ``psql``.
_RECEIPT_EXIT_UNKNOWN = 3

#: How recently a restore must have rewritten the database, when nothing says
#: otherwise. Deliberately *not* ``database.restore.max_age_hours``, which asks a
#: different question — how old the backup *file* may be. A 48h tolerance for an
#: input is a reasonable thing to configure and a uselessly loose window for "did
#: last night's restore run": a nightly that stopped a day ago would still pass.
#: 26h suits a nightly cadence — a couple of hours of slack, and no more.
_DEFAULT_ACTUATION_WINDOW_HOURS = 26.0


@db.command(name="receipt")
@click.argument("fraise")
@click.argument("environment")
@click.option(
    "--max-age-hours",
    type=float,
    default=None,
    help=(
        "How recently a restore must have rewritten the database. Defaults to "
        "the fraise's database.restore.max_actuation_age_hours "
        f"(default {_DEFAULT_ACTUATION_WINDOW_HOURS:g}h)."
    ),
)
@click.option(
    "--check-heap",
    is_flag=True,
    help=(
        "Also report relation-file mtimes. Needs superuser or "
        "pg_read_server_files; corroborates the receipt, never overrides it."
    ),
)
@click.option(
    "--heap-schema",
    default=None,
    help=(
        "Schema whose base tables --check-heap inspects. One schema, never "
        "summed. Defaults to the schema the restore derived its floor for, as "
        "recorded in the receipt, and to 'public' when the receipt names none."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Emit the verdict as JSON")
@click.pass_context
def db_receipt(
    ctx: click.Context,
    fraise: str,
    environment: str,
    max_age_hours: float | None,
    check_heap: bool,
    heap_schema: str | None,
    as_json: bool,
) -> None:
    """Ask a database when a fraisier restore last rewrote it.

    Every count taken on a stale database is correct — that is what makes a
    nightly restore that silently stopped running so hard to notice (#343,
    #356). Each restore leaves a token behind; this reads it back and reports
    how long ago the run that left it finished.

    \b
    Exit codes:
      0  a restore rewrote this database inside the window
      1  the last restore is older than the window — staging is stale
      3  no receipt, or the check could not run. Not checked, not passed.

    \b
    Examples:
        fraisier db receipt api staging
        fraisier db receipt api staging --max-age-hours 12 --json
    """
    from fraisier.dbops.guard import is_external_db
    from fraisier.dbops.receipt import ActuationVerdict, verify_actuation

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
    db_name = db_cfg.get("name", fraise)
    admin_url = db_cfg.get("admin_url")
    if not admin_url:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' env '{environment}' has no "
            "admin_url; set database.admin_url in fraise/env/*.yaml"
        )
        raise SystemExit(1)

    # Its own key, not the restore's `max_age_hours`. That one bounds the age of
    # the backup *file* the restore is allowed to load; this one bounds how long
    # ago a restore last ran. On a nightly cadence they are different numbers,
    # and borrowing the input tolerance for the output question makes the window
    # far too loose to notice the failure it exists to catch.
    window = max_age_hours
    if window is None:
        window = float(
            (db_cfg.get("restore") or {}).get(
                "max_actuation_age_hours", _DEFAULT_ACTUATION_WINDOW_HOURS
            )
        )

    check = verify_actuation(db_name, connection_url=admin_url, max_age_hours=window)
    receipt = check.receipt

    # The receipt names where this database's heaps actually live, because the
    # restore derived it from the archive and nothing else can. Guessing
    # `public` on a host whose tables are in `tenant` finds no base tables and
    # returns UNVERIFIABLE — silence, precisely where the cross-check is most
    # wanted. That is the mismatch the v0.64.0 floor fix removed, and it is not
    # being reintroduced one command over. Resolved unconditionally so the JSON
    # can report which schema was looked in.
    schema = heap_schema or (receipt.floor_schema if receipt else None) or "public"

    # Opt-in, and reported as a line rather than folded into the verdict. It
    # answers a related but weaker question — mtimes move for autovacuum too, so
    # it can pass a database the receipt correctly calls stale — and it needs a
    # privilege most managed PostgreSQL will not grant, which would otherwise
    # print "could not read" on every run. When the two disagree both are shown:
    # "the receipt says today and the heap says last week" is something an
    # operator wants told, not something this should settle silently.
    heap = None
    if check_heap:
        from fraisier.dbops.receipt import relation_freshness

        heap = relation_freshness(
            db_name,
            schema=schema,
            connection_url=admin_url,
            within_hours=window,
        )

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "database": db_name,
                    "verdict": check.verdict.value,
                    "detail": check.detail,
                    "max_age_hours": window,
                    "run_id": receipt.run_id if receipt else None,
                    "backup_path": receipt.backup_path if receipt else None,
                    "backup_bytes": receipt.backup_bytes if receipt else None,
                    "restored_at": receipt.restored_at.isoformat() if receipt else None,
                    "age_hours": receipt.age_hours if receipt else None,
                    "floor_schema": receipt.floor_schema if receipt else None,
                    # The schema is reported alongside the verdict because it is
                    # resolved rather than given: an operator reading
                    # UNVERIFIABLE needs to see which schema was looked in.
                    "heap": (
                        {
                            "verdict": heap.verdict.value,
                            "detail": heap.detail,
                            "schema": schema,
                        }
                        if heap
                        else None
                    ),
                },
                indent=2,
            )
        )
    elif check.verdict is ActuationVerdict.ACTUATED and receipt:
        console.print(
            f"[green]Actuated:[/green] {db_name} was rewritten "
            f"{receipt.age_hours:.1f}h ago by run {receipt.run_id}"
        )
        console.print(f"  From backup: {receipt.backup_path}")
    elif check.verdict is ActuationVerdict.STALE:
        console.print(f"[red]Stale:[/red] {check.detail}")
        if receipt:
            console.print(f"  Last backup restored: {receipt.backup_path}")
    else:
        console.print(f"[yellow]Not checked:[/yellow] {check.detail}")
        console.print(
            "  This says nothing either way about the database — it is not a "
            "passed check and not a failed one."
        )

    if heap is not None and not as_json:
        colour = {
            ActuationVerdict.ACTUATED: "green",
            ActuationVerdict.STALE: "red",
        }.get(heap.verdict, "yellow")
        console.print(f"  [{colour}]Heap mtimes:[/{colour}] {heap.detail}")

    # The receipt decides. The heap check corroborates and does not vote: it
    # passes a stale database whose autovacuum happened to run, so letting it
    # move the exit code would trade a reliable signal for an unreliable one.
    if check.verdict is ActuationVerdict.ACTUATED:
        return
    raise SystemExit(1 if check.is_bad else _RECEIPT_EXIT_UNKNOWN)


class _BackupGroup(click.Group):
    """``fraisier backup <fraise>`` predates ``fraisier backup prune``.

    The documented, tested form takes a fraise as its first positional
    argument, which a plain :class:`click.Group` would try to resolve as a
    subcommand name and reject. Anything that is not a known subcommand is
    therefore routed to ``run``, keeping both surfaces exact.

    The one cost: a fraise named after a subcommand is unreachable through
    the legacy form. ``prune`` is a poor fraise name and ``fraisier backup
    run prune -e …`` still reaches it, so this is a documented shadow
    rather than a lost capability.
    """

    def resolve_command(self, ctx, args):
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args = ["run", *args]
        return super().resolve_command(ctx, args)


@main.group(name="backup", cls=_BackupGroup)
def backup_group() -> None:
    """Database backup and retention commands.

    \b
    Examples:
        fraisier backup my_api -e production
        fraisier backup prune -e development
    """


@backup_group.command(name="run")
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


def _select_retain_entries(config, env: str, name: str | None):
    """The entries ``prune`` should act on, or a message saying why none.

    Every "nothing to do" case is an error rather than a quiet exit 0. The
    incident this closes is a story about work that did not happen
    reporting success, and a timer that exits 0 having pruned nothing is
    indistinguishable from one that pruned correctly.
    """
    entries = config.retain_entries(env)
    if not entries:
        declared = sorted({e.environment for e in config.all_retain_entries()})
        known = ", ".join(declared) if declared else "(none)"
        return None, (
            f"No retention policy for environment '{env}'. "
            f"Environments with a backup.environments.<env>.retain block: {known}"
        )
    if name is None:
        return entries, None

    selected = [e for e in entries if e.name == name]
    if not selected:
        names = ", ".join(sorted(e.name for e in entries))
        return None, (
            f"No retention entry named '{name}' in environment '{env}'. Known: {names}"
        )
    return selected, None


def _prune_one(entry, *, dry_run: bool):
    """Apply one entry, or report why it could not be applied."""
    from fraisier.dbops.backup import cleanup_old_backups

    directory = Path(entry.dir)
    if not directory.is_dir():
        return None, (
            f"{entry.name}: {entry.dir} is not a directory. A retention policy "
            f"pointed at a path that is not there prunes nothing, every night, "
            f"reporting success"
        )
    outcome = cleanup_old_backups(
        directory,
        retention_hours=entry.retention_hours,
        match=entry.match,
        keep_minimum=entry.keep_minimum,
        dry_run=dry_run,
    )
    return outcome, None


def _low_disk_warning(entry) -> str | None:
    """The corpus volume is below the entry's declared floor (#344).

    A warning and not a failure, deliberately: a non-zero exit here converts a
    disk warning into a failed unit and stops the pruning that is the one thing
    that might still help. Returns None when no threshold is declared — which is
    every config written before the field — or when the volume cannot be read,
    since "I could not measure" is not "the disk is full" any more than it is
    "there is room".
    """
    if entry.min_free_gb is None:
        return None
    from fraisier.dbops.backup import free_space_gb

    try:
        free_gb = free_space_gb(entry.dir)
    except OSError:
        return None
    if free_gb >= entry.min_free_gb:
        return None
    return (
        f"WARNING: {entry.dir} has {free_gb:.1f}GB free, below the "
        f"min_free_gb={entry.min_free_gb} declared for {entry.name}. Retention "
        f"bounds this corpus but cannot recover a disk something else filled."
    )


def _unreadable_dump_warning(entry, outcome) -> str:
    """Name the dumps the floor refused to protect (#342).

    Goes through the same stderr channel as the stalled-producer warning rather
    than relying on `cleanup_old_backups`'s log line: without `-v` the root
    logger has no handler, so that line only reaches stderr via
    `logging.lastResort` — which any earlier `basicConfig` call would silently
    redirect. The two warnings together describe the #339 state completely:
    nothing recent is arriving *and* what did arrive cannot be read.
    """
    names = ", ".join(Path(p).name for p in outcome.invalid)
    return (
        f"WARNING: {len(outcome.invalid)} backup(s) in {entry.dir} are not "
        f"readable archives and were not allowed to hold a keep_minimum slot: "
        f"{names}. A dump pg_restore cannot list cannot be restored from — "
        f"check the transfer from the producer."
    )


def _stalled_producer_warning(entry, outcome) -> str:
    """What `floor_was_load_bearing` means, said in the operator's terms."""
    newest_age = ""
    survivors = [Path(p) for p in outcome.exempted_by_minimum]
    if survivors:
        newest = max(survivors, key=lambda p: p.stat().st_mtime)
        hours = (time.time() - newest.stat().st_mtime) / 3600
        newest_age = f" The newest is {hours:.0f}h old ({newest.name})."
    return (
        f"WARNING: every backup in {entry.dir} is past its retention window; "
        f"only keep_minimum={entry.keep_minimum} is holding the corpus open."
        f"{newest_age} Nothing recent has arrived — check the producer."
    )


@backup_group.command(name="prune")
@click.option("--env", "-e", required=True, help="Environment whose policy to apply")
@click.option("--name", default=None, help="Apply one entry rather than all of them")
@click.option(
    "--dry-run", is_flag=True, help="List what would be removed, remove nothing"
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable report")
@click.pass_context
def backup_prune(
    ctx: click.Context, env: str, name: str | None, dry_run: bool, as_json: bool
) -> None:
    """Apply the retention policy for a backup corpus this host receives.

    Deletion runs on the receiving host and only there: no part of this
    reaches back to the producer, so a compromised sender key cannot erase
    the corpus it pushed.

    \b
    Examples:
        fraisier backup prune -e development
        fraisier backup prune -e development --name production-full
        fraisier backup prune -e development --dry-run
    """
    config = ctx.obj["config"]

    entries, problem = _select_retain_entries(config, env, name)
    if entries is None:
        console.print(f"[red]Error:[/red] {problem}")
        raise SystemExit(1)

    reports: list[dict] = []
    failures: list[str] = []
    warnings: list[str] = []

    for entry in entries:
        outcome, failure = _prune_one(entry, dry_run=dry_run)
        if outcome is None:
            failures.append(failure or "")
            continue
        reports.append(
            {
                "name": entry.name,
                "dir": entry.dir,
                "match": entry.match,
                "retention_days": entry.retention_days,
                "keep_minimum": entry.keep_minimum,
                "dry_run": dry_run,
                "removed": list(outcome.removed),
                "kept": list(outcome.kept),
                "exempted_by_minimum": list(outcome.exempted_by_minimum),
                "floor_was_load_bearing": outcome.floor_was_load_bearing,
                # Overlay, not a fourth partition member: every name here also
                # appears in exactly one of removed/kept/exempted_by_minimum.
                "invalid": list(outcome.invalid),
            }
        )
        if outcome.invalid:
            warnings.append(_unreadable_dump_warning(entry, outcome))
        if outcome.floor_was_load_bearing:
            warnings.append(_stalled_producer_warning(entry, outcome))
        low_disk = _low_disk_warning(entry)
        if low_disk:
            warnings.append(low_disk)

    if as_json:
        # Nothing else may reach stdout: the report is piped, and a Rich
        # panel in front of it is a parse error rather than a formatting
        # nuisance. Warnings and failures go to stderr.
        click.echo(_json.dumps({"environment": env, "entries": reports}, indent=2))
    else:
        for report in reports:
            verb = "would remove" if dry_run else "removed"
            console.print(
                f"[cyan]{report['name']}[/cyan] ({report['dir']}): "
                f"{verb} {len(report['removed'])}, kept {len(report['kept'])}, "
                f"floor held {len(report['exempted_by_minimum'])}"
            )
            for path in report["removed"]:
                console.print(f"    - {path}")

    for warning in warnings:
        click.echo(warning, err=True)

    if failures:
        for failure in failures:
            click.echo(f"Error: {failure}", err=True)
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

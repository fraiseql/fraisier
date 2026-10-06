"""``fraisier db tviews`` — inspect and rebuild pg_tviews TVIEWs (#422).

fraisier does not drive a failover or a crash restart, so the step that follows
one is an operator command: promote, then ``db tviews rebuild``.  An UNLOGGED
TVIEW is empty after either, every count passes, and the read model is gone.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import click

from ._helpers import console
from .db import _get_db_config, db

if TYPE_CHECKING:
    from fraisier.dbops.tviews import EmptyTview

#: ``status`` could not reach a verdict.  Not 0: a host that could not look must
#: not report what a host that looked and passed reports, and not 1, because a
#: monitoring timer that pages on an empty TVIEW should not page on a host that
#: lacks pg_tviews.  Same contract as ``db receipt``.
EXIT_NOT_CHECKED = 3


def _database_url(ctx: click.Context, fraise: str, env: str) -> str:
    """The URL of *fraise*'s database in *env*, or exit 1 saying why."""
    from fraisier.dbops._url import replace_db_name, resolve_db_url
    from fraisier.dbops.guard import is_external_db

    config = ctx.obj["config"]
    fraise_cfg, env_config = _get_db_config(config, fraise, env)
    if not fraise_cfg or not env_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{env}' not found"
        )
        raise SystemExit(1)
    if is_external_db(fraise_cfg):
        console.print(f"[yellow]Skipping '{fraise}': external_db is true[/yellow]")
        raise SystemExit(0)

    db_cfg = env_config.get("database") or {}
    url = resolve_db_url(db_cfg.get("database_url"))
    if not url:
        admin_url = resolve_db_url(db_cfg.get("admin_url"), role="admin_url")
        if admin_url:
            url = replace_db_name(admin_url, db_cfg.get("name", fraise))
    if not url:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' env '{env}' has no "
            "database.database_url (or admin_url) to connect to"
        )
        raise SystemExit(1)
    return url


@db.group(name="tviews")
def tviews_group() -> None:
    """Inspect and rebuild pg_tviews TVIEWs.

    \b
    Examples:
        fraisier db tviews status api -e production
        fraisier db tviews rebuild api -e production
    """


@tviews_group.command(name="status")
@click.argument("fraise")
@click.option("--env", "-e", required=True, help="Target environment")
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON")
@click.pass_context
def tviews_status(ctx: click.Context, fraise: str, env: str, as_json: bool) -> None:
    """Show each TVIEW's size and health, and which are empty under a full view.

    Reads ``pg_tviews_profile()``, which is read-only and callable on a standby,
    then probes each TVIEW against its backing view.  An UNLOGGED TVIEW cannot be
    read on a standby, so there the emptiness check is reported as unavailable
    rather than guessed.

    \b
    Exit codes:
      0  every TVIEW that should have rows has them
      1  a TVIEW is empty while its backing view has rows — rebuild it
      3  could not check: no pg_tviews, an older pg_tviews, or no connection

    \b
    Examples:
        fraisier db tviews status api -e production
        fraisier db tviews status api -e production --json
    """
    import psycopg

    from fraisier.dbops import tviews

    url = _database_url(ctx, fraise, env)
    try:
        if not tviews.tviews_installed(url):
            _not_checked(as_json, "pg_tviews is not installed in this database")
        profile = tviews.profile_tviews(url)
    except (tviews.TviewError, psycopg.Error) as exc:
        _not_checked(as_json, str(exc).splitlines()[0] if str(exc) else repr(exc))

    empty: list[tviews.EmptyTview] | None
    try:
        empty = tviews.find_empty_tviews(url)
    except tviews.TviewError, psycopg.Error:
        empty = None

    if as_json:
        click.echo(
            json.dumps(
                {
                    "tviews": profile,
                    "empty": [{"tview": e.tview, "view": e.view} for e in empty or ()],
                    "emptiness_checked": empty is not None,
                },
                indent=2,
                default=str,
            )
        )
    else:
        _print_status(profile, empty)
    if empty:
        raise SystemExit(1)


def _not_checked(as_json: bool, reason: str) -> None:
    if as_json:
        click.echo(json.dumps({"tviews": [], "error": reason}, indent=2))
    else:
        console.print(f"[yellow]Not checked:[/yellow] {reason}")
        console.print(
            "  This says nothing either way about the TVIEWs — it is not a "
            "passed check and not a failed one."
        )
    raise SystemExit(EXIT_NOT_CHECKED)


def _print_status(
    profile: list[dict[str, object]], empty: list[EmptyTview] | None
) -> None:
    from rich.table import Table

    table = Table("entity", "tview", "persistence", "rows (est.)", "warnings")
    for row in profile:
        warnings = row.get("warnings") or []
        table.add_row(
            str(row.get("entity")),
            str(row.get("tview")),
            str(row.get("persistence")),
            "-" if row.get("rows_estimate") is None else str(row["rows_estimate"]),
            "; ".join(str(w) for w in warnings) if isinstance(warnings, list) else "",
        )
    console.print(table)
    if empty is None:
        console.print(
            "[yellow]Emptiness not checked:[/yellow] the TVIEWs could not be read "
            "(a standby cannot read an UNLOGGED table)"
        )
    elif empty:
        for found in empty:
            console.print(
                f"[red]Empty:[/red] {found.tview} has no rows, its view "
                f"{found.view} does"
            )
        console.print("  Run `fraisier db tviews rebuild` on the primary.")
    else:
        console.print("[green]No TVIEW is empty under a populated view.[/green]")


@tviews_group.command(name="rebuild")
@click.argument("fraise")
@click.option("--env", "-e", required=True, help="Target environment")
@click.option(
    "--all",
    "everything",
    is_flag=True,
    help=(
        "Rebuild every TVIEW, not only the empty UNLOGGED ones. For a LOGGED "
        "TVIEW the probe reports, or one you suspect is stale."
    ),
)
@click.option(
    "--skip-if-locked",
    is_flag=True,
    help="Exit 0 instead of failing when a deploy holds the lock.",
)
@click.pass_context
def tviews_rebuild(
    ctx: click.Context, fraise: str, env: str, everything: bool, skip_if_locked: bool
) -> None:
    """Rebuild TVIEWs from their backing views — the step after a failover.

    Run it on the primary once it is promoted, or after a crash-recovery start:
    an UNLOGGED TVIEW is empty after either and nothing else notices.  By default
    only TVIEWs that are empty while their view has rows are filled
    (``pg_tviews_rebuild_all(only_empty)``), dependencies first and without
    ``TRUNCATE``, so readers are not blocked.  On a standby pg_tviews refuses,
    and this says so and exits non-zero.

    Holds the same per-fraise deployment lock the webhook uses.

    \b
    Examples:
        fraisier db tviews rebuild api -e production
        fraisier db tviews rebuild api -e production --all
        fraisier db tviews rebuild api -e production --skip-if-locked
    """
    import psycopg

    from fraisier.dbops import tviews
    from fraisier.errors import DeploymentLockError
    from fraisier.locking import deployment_lock

    url = _database_url(ctx, fraise, env)
    try:
        with deployment_lock(fraise):
            if not tviews.tviews_installed(url):
                console.print(
                    "[yellow]pg_tviews is not installed in this database[/yellow]"
                )
                raise SystemExit(EXIT_NOT_CHECKED)
            rebuilt = (
                tviews.rebuild_all_tviews(url)
                if everything
                else tviews.rebuild_empty_tviews(url)
            )
    except DeploymentLockError as exc:
        if skip_if_locked:
            console.print(f"[yellow]Skipping rebuild:[/yellow] {exc}")
            return
        console.print(f"[red]Error:[/red] {exc}")
        console.print(
            "  A deploy is in progress for this fraise. Retry once it finishes, "
            "or pass --skip-if-locked."
        )
        raise SystemExit(1) from exc
    except tviews.TviewError as exc:
        console.print(f"[red]Rebuild refused:[/red] {exc}")
        raise SystemExit(1) from exc
    except psycopg.Error as exc:
        console.print(f"[red]Rebuild failed:[/red] {exc}")
        raise SystemExit(1) from exc

    if not rebuilt:
        console.print("Nothing to rebuild: no TVIEW was empty under a populated view.")
        return
    for entry in rebuilt:
        console.print(f"[green]Rebuilt[/green] {entry.entity}: {entry.rows} row(s)")

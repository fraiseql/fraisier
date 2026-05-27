"""``fraisier env-check <subcommand>`` — preflight env-var report (#221).

Static CI-friendly preflight: given a subcommand, walk its config
sections and report every reachable ``!envvar`` reference, marking
which are currently unset. Exit non-zero when any required variable
is missing.

Text mode is a rich.Table; ``--format json`` emits a stable structured
payload designed for piping into ``jq`` or similar.
"""

from __future__ import annotations

import json as _json
from typing import Any

import click
from rich.table import Table

from fraisier.introspection import (
    COMMANDS_WITHOUT_CONFIG_ACCESS,
    SUBCOMMAND_CONFIG_SECTIONS,
    EnvVarRef,
    reachable_envvars,
)

from ._helpers import console, err_console, require_config
from .main import main


def _valid_subcommands() -> set[str]:
    return set(SUBCOMMAND_CONFIG_SECTIONS.keys()) | set(COMMANDS_WITHOUT_CONFIG_ACCESS)


def _render_table(refs: list[EnvVarRef], unset_count: int, total: int) -> Table:
    table = Table(
        title=f"Reads {total} envvars ({unset_count} unset)",
        title_style="cyan",
    )
    table.add_column("Var", style="bold")
    table.add_column("Path", style="dim")
    table.add_column("Status")
    for ref in refs:
        status = "[green]set[/green]" if ref.is_set else "[red]UNSET[/red]"
        table.add_row(ref.name, ref.yaml_path, status)
    return table


def _payload(
    refs: list[EnvVarRef],
    subcommand: str,
    fraise: str | None,
    environment: str | None,
) -> dict[str, Any]:
    return {
        "subcommand": subcommand,
        "fraise": fraise,
        "environment": environment,
        "envvars": [
            {"name": r.name, "yaml_path": r.yaml_path, "is_set": r.is_set} for r in refs
        ],
        "all_set": all(r.is_set for r in refs),
        "unset_count": sum(1 for r in refs if not r.is_set),
    }


@main.command(name="env-check")
@click.argument("subcommand")
@click.option("--fraise", "-f", default=None, help="Narrow to one fraise")
@click.option("--environment", "-e", default=None, help="Narrow to one environment")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text)",
)
@click.option(
    "--required-only",
    is_flag=True,
    help="Only list unset variables (CI-friendly terse output)",
)
@click.pass_context
def env_check(
    ctx: click.Context,
    subcommand: str,
    fraise: str | None,
    environment: str | None,
    fmt: str,
    required_only: bool,
) -> None:
    """Report env vars a given subcommand would read.

    \b
    Examples:
        fraisier env-check ship
        fraisier env-check trigger-deploy --fraise my_api --environment production
        fraisier env-check ship --format json | jq '.unset_count'
        fraisier env-check ship --required-only
    """
    valid = _valid_subcommands()
    if subcommand not in valid:
        err_console.print(
            f"[red]Error:[/red] Unknown subcommand {subcommand!r}.\n"
            f"Valid: {', '.join(sorted(valid))}"
        )
        raise SystemExit(2)

    config = require_config(ctx)
    raw = getattr(config, "_config", None)
    refs = reachable_envvars(
        raw if isinstance(raw, dict) else None,
        subcommand,
        fraise=fraise,
        environment=environment,
    )

    if required_only:
        refs = [r for r in refs if not r.is_set]

    unset_count = sum(1 for r in refs if not r.is_set)
    total = len(refs)

    if fmt == "json":
        payload = _payload(refs, subcommand, fraise, environment)
        if required_only:
            payload["envvars"] = [r for r in payload["envvars"] if not r["is_set"]]
        click.echo(_json.dumps(payload, indent=2))
    elif not refs:
        console.print(
            f"[dim]Subcommand {subcommand!r} reads no envvars "
            f"from the loaded config.[/dim]"
        )
    else:
        console.print(_render_table(refs, unset_count, total))

    if unset_count:
        ctx.exit(1)

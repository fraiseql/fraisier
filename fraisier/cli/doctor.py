"""``fraisier doctor`` — host-wide self-diagnosis CLI (#221 bundle B).

Thin Click wrapper around ``fraisier.doctor.run_all``. Renders text
output via rich (one line per check, optional fix-hint follow-up line),
or stable JSON via ``--format json``.

Exit code convention:
- 0: all checks pass (skip counts as pass)
- 1: any check returned ``fail``
- 2: any check returned ``warn`` but no ``fail``
"""

from __future__ import annotations

import json as _json
from typing import TYPE_CHECKING, Any

import click

from fraisier import doctor

from ._helpers import console
from .main import main

if TYPE_CHECKING:
    from fraisier.doctor import CheckResult

_STATUS_GLYPH = {
    "pass": "[green]✓[/green]",
    "warn": "[yellow]⚠[/yellow]",
    "fail": "[red]✗[/red]",
    "skip": "[dim]-[/dim]",
}


def render_text(results: list[CheckResult]) -> None:
    name_width = max((len(r.name) for r in results), default=10) + 2
    for r in results:
        glyph = _STATUS_GLYPH[r.status]
        console.print(f"  {glyph} {r.name:<{name_width}}{r.detail}")
        if r.fix_hint:
            console.print(f"    [dim]fix: {r.fix_hint}[/dim]")
    summary = doctor.summarize(results)
    console.print()
    console.print(
        f"  [bold]{summary['fail']} fail, {summary['warn']} warn, "
        f"{summary['pass']} pass, {summary['skip']} skip[/bold]"
    )


def render_json(results: list[CheckResult]) -> dict[str, Any]:
    from importlib.metadata import version

    try:
        fraisier_version = version("fraisier")
    except Exception:
        fraisier_version = "unknown"
    return {
        "fraisier_version": fraisier_version,
        "checks": [
            {
                "name": r.name,
                "status": r.status,
                "detail": r.detail,
                "fix_hint": r.fix_hint,
            }
            for r in results
        ],
        "summary": doctor.summarize(results),
    }


@main.command(name="doctor")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (default: text)",
)
@click.option(
    "--check",
    "only",
    multiple=True,
    help="Run only the named check(s); repeatable",
)
@click.option(
    "--skip-network",
    is_flag=True,
    help="Skip checks that hit the network or external binaries",
)
@click.option(
    "--probe-sandbox",
    is_flag=True,
    help=(
        "Also run the active sandbox write probe: spawns a transient "
        "ProtectSystem=strict unit over the rendered ReadWritePaths= and "
        "writes into each one. Needs root; skipped cleanly without it."
    ),
)
@click.pass_context
def doctor_cmd(
    ctx: click.Context,
    fmt: str,
    only: tuple[str, ...],
    skip_network: bool,
    probe_sandbox: bool,
) -> None:
    """Run host-wide health checks and report fraisier install state.

    \b
    Examples:
        fraisier doctor
        fraisier doctor --format json | jq '.summary'
        fraisier doctor --check python_version --check fraisier_version
        fraisier doctor --skip-network --format json
        sudo fraisier doctor --probe-sandbox   # before scaffold-install
    """
    config = ctx.obj.get("config") if ctx.obj else None

    results = doctor.run_all(
        config,
        only=list(only) if only else None,
        skip_network=skip_network,
        probe_sandbox=probe_sandbox,
    )

    if fmt == "json":
        click.echo(_json.dumps(render_json(results), indent=2))
    else:
        render_text(results)

    summary = doctor.summarize(results)
    if summary["fail"]:
        ctx.exit(1)
    elif summary["warn"]:
        ctx.exit(2)

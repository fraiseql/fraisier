"""``fraisier scheduled-install`` CLI command.

Flat command (symmetric with ``fraisier scaffold-install``), wired here and
registered through the import chain in ``fraisier/cli/main.py``.

Operator runs ``sudo fraisier scheduled-install --env <env>`` to lay down the
per-job systemd unit files that ``type: scheduled`` fraises declare in
``fraises.yaml``, daemon-reload, and ``enable --now`` each timer. Idempotent
on re-run.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import TYPE_CHECKING

import click

from fraisier.runners import LocalRunner
from fraisier.scheduled_install import (
    PrunePlan,
    ScheduledInstallError,
    UnitDiff,
    UnitState,
    apply_unit_diffs,
    apply_unit_diffs_via_helper,
    classify_unit,
    enumerate_scheduled_units,
    prune_orphans,
)

from ._helpers import console, err_console, require_config
from .main import main

if TYPE_CHECKING:
    from fraisier.config import FraisierConfig


# Exit codes, mirroring the table in phase-04-cli.md:
#   0 = success (including idempotent no-op)
#   1 = operator error (MISSING_SOURCE, unparseable yaml, etc.)
#   2 = policy violation (missing --env, DRIFTED without --force, unknown env)
EXIT_OK = 0
EXIT_OPERATOR_ERROR = 1
EXIT_POLICY_VIOLATION = 2


def _scheduled_environments(config: FraisierConfig) -> list[tuple[str, str]]:
    """Return [(env_name, fraise_name), ...] for every env on type:scheduled fraises."""
    pairs: list[tuple[str, str]] = []
    for fraise_name, fraise in config.fraises.items():
        if fraise.get("type") != "scheduled":
            continue
        pairs.extend(
            (env_name, fraise_name) for env_name in fraise.get("environments") or {}
        )
    return pairs


def _format_env_list(pairs: list[tuple[str, str]]) -> str:
    """Format the available-envs list for the ``--env`` missing/unknown error."""
    if not pairs:
        return "  (no type:scheduled fraises declared in this fraises.yaml)"
    lines = []
    for env_name, fraise_name in sorted(set(pairs)):
        lines.append(f"  {env_name:<12} (from fraise `{fraise_name}`)")
    return "\n".join(lines)


def _print_dry_run(diffs: list[UnitDiff]) -> None:
    """Render the would-do plan to stdout. Mirrors the format in phase-04-cli.md.

    Uses ``markup=False`` for the structural lines because they contain
    ``[would copy]`` / ``[production]`` square brackets that rich would
    otherwise interpret (and silently discard) as markup tags.
    """
    console.print("fraisier scheduled-install (dry-run)")

    # Group by (fraise_name, environment, job_name) for readable output.
    by_group: dict[tuple[str, str, str], list[UnitDiff]] = {}
    for d in diffs:
        key = (d.install.fraise_name, d.install.environment, d.install.job_name)
        by_group.setdefault(key, []).append(d)

    write_count = 0
    timer_count = 0
    for (fraise_name, env, job_name), group in by_group.items():
        console.print(f"  {fraise_name} [{env}] / {job_name}", markup=False)
        for d in group:
            console.print(f"    [would copy]    {d.install.source_path}", markup=False)
            console.print(
                f"                 -> {d.install.dest_path}     ({d.state.value})",
                markup=False,
            )
            if d.state is UnitState.ABSENT or d.state is UnitState.DRIFTED:
                write_count += 1
                if d.install.is_timer:
                    timer_count += 1

    if write_count > 0:
        console.print("  [would run]    systemctl daemon-reload", markup=False)
        for d in diffs:
            if d.state in (UnitState.ABSENT, UnitState.DRIFTED) and d.install.is_timer:
                console.print(
                    f"  [would run]    systemctl enable --now {d.install.unit_name}",
                    markup=False,
                )

    if write_count == 0:
        console.print("Nothing to do (all units identical).")
    else:
        timer_word = "timer" if timer_count == 1 else "timers"
        summary = (
            f"{write_count} units would be installed, "
            f"{timer_count} {timer_word} enabled."
        )
        console.print(summary)


def _print_verbose_diff(diff: UnitDiff) -> None:
    """Print a full unified diff for one DRIFTED unit (called under --verbose)."""
    src = diff.install.source_path.read_text(encoding="utf-8", errors="replace")
    dst = diff.install.dest_path.read_text(encoding="utf-8", errors="replace")
    lines = difflib.unified_diff(
        dst.splitlines(keepends=True),
        src.splitlines(keepends=True),
        fromfile=str(diff.install.dest_path),
        tofile=str(diff.install.source_path),
        lineterm="",
    )
    console.print("".join(lines))


@main.command(name="scheduled-install")
@click.option(
    "--env",
    "env",
    default=None,
    help="Target environment (required). Validated against type:scheduled fraises.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the would-be plan and exit without writing.",
)
@click.option(
    "--validate-only",
    is_flag=True,
    help="Classify units only; exit non-zero if any DRIFTED or MISSING_SOURCE.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite DRIFTED dest files. Without this, drift is a hard error.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the 'install N units into /etc/systemd/system/' confirmation.",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Print full unified diffs for DRIFTED units.",
)
@click.option(
    "--fraise",
    default=None,
    help="Narrow to a single fraise name (otherwise: all type:scheduled fraises).",
)
@click.option(
    "--via-socket",
    "via_socket",
    is_flag=True,
    help=(
        "Route the apply through the unit-installer socket helper instead of "
        "writing /etc/systemd/system/ directly. Drops the sudo requirement. "
        "Helper must be on the host (requires `fraisier scaffold-install`)."
    ),
)
@click.option(
    "--socket-path",
    "socket_path_override",
    default=None,
    help=(
        "Override the helper socket path. Defaults to "
        "/run/fraisier/<env>/unit-installer-<project>.sock."
    ),
)
@click.option(
    "--prune",
    "prune",
    is_flag=True,
    help=(
        "Disable + remove orphan units (those still on disk under their "
        "fraisier-managed marker but no longer declared in fraises.yaml). "
        "Per-env scoped. Requires --yes (or an interactive TTY) to confirm."
    ),
)
@click.pass_context
def scheduled_install_cmd(
    ctx: click.Context,
    env: str | None,
    dry_run: bool,
    validate_only: bool,
    force: bool,
    yes: bool,
    verbose: bool,
    fraise: str | None,
    via_socket: bool,
    socket_path_override: str | None,
    prune: bool,
) -> None:
    """Install per-job systemd unit files declared by type:scheduled fraises.

    Reads each declared unit from ``<app_path>/scripts/systemd/<unit>``,
    copies to ``/etc/systemd/system/``, ``daemon-reload``s, and
    ``enable --now``s each timer. Idempotent on re-run.

    Run under ``sudo`` — the file copy and systemctl invocations both need root.

    \b
    Exit codes:
        0 - Success (including idempotent re-run)
        1 - Operator error: source missing, unparseable yaml
        2 - Policy: missing --env, drift without --force, unknown env

    \b
    Examples:
        fraisier scheduled-install --env production --dry-run
        fraisier scheduled-install --env production --validate-only
        fraisier scheduled-install --env production --yes
        fraisier scheduled-install --env production --force --yes
    """
    config = require_config(ctx)
    available = _scheduled_environments(config)

    if env is None:
        err_console.print(
            "[red]Error:[/red] --env is required. "
            "Available environments for type:scheduled fraises:"
        )
        err_console.print(_format_env_list(available))
        ctx.exit(EXIT_POLICY_VIOLATION)

    valid_envs = {e for e, _ in available}
    if env not in valid_envs:
        err_console.print(
            f"[red]Error:[/red] env [bold]{env}[/bold] is not declared on any "
            "type:scheduled fraise. Available environments:"
        )
        err_console.print(_format_env_list(available))
        ctx.exit(EXIT_POLICY_VIOLATION)

    if prune:
        if via_socket:
            err_console.print(
                "[red]Error:[/red] --prune + --via-socket isn't supported in "
                "v0.29; run with operator-typed sudo for now. Planned for v0.30."
            )
            ctx.exit(EXIT_POLICY_VIOLATION)
        plans = prune_orphans(config, env)
        _run_prune(plans, ctx, yes=yes, dry_run=dry_run)
        return

    units = enumerate_scheduled_units(config, env)
    if fraise is not None:
        units = [u for u in units if u.fraise_name == fraise]
        if not units:
            err_console.print(
                f"[red]Error:[/red] no type:scheduled units found for "
                f"fraise=[bold]{fraise}[/bold] env=[bold]{env}[/bold]."
            )
            ctx.exit(EXIT_POLICY_VIOLATION)

    if not units:
        console.print(
            f"No type:scheduled units declared for env [bold]{env}[/bold]. "
            "Nothing to do."
        )
        ctx.exit(EXIT_OK)

    diffs: list[UnitDiff] = [classify_unit(u) for u in units]

    if validate_only:
        _run_validate_only(diffs, ctx, verbose=verbose)
        return

    if dry_run:
        _print_dry_run(diffs)
        # Mirror apply's failure modes so dry-run exit code is honest.
        if any(d.state is UnitState.MISSING_SOURCE for d in diffs):
            ctx.exit(EXIT_OPERATOR_ERROR)
        if not force and any(d.state is UnitState.DRIFTED for d in diffs):
            ctx.exit(EXIT_POLICY_VIOLATION)
        ctx.exit(EXIT_OK)

    # Real apply path.
    _run_apply(
        diffs,
        ctx,
        force=force,
        yes=yes,
        verbose=verbose,
        via_socket=via_socket,
        socket_path=_resolve_socket_path(config, env, override=socket_path_override)
        if via_socket
        else None,
        config_path=Path(config.config_path) if via_socket else None,
    )


def _run_prune(
    plans: list[PrunePlan],
    ctx: click.Context,
    *,
    yes: bool,
    dry_run: bool,
) -> None:
    """--prune flow. v0.29 uses the operator-sudo path (direct fs + systemctl)."""
    orphan_count = sum(1 for p in plans if p.kind == "orphan")
    stale_count = sum(1 for p in plans if p.kind == "stale_marker")

    if not plans:
        console.print("No orphan units to prune. Already converged.")
        ctx.exit(EXIT_OK)

    console.print(
        f"fraisier scheduled-install --prune ({'dry-run' if dry_run else 'apply'})"
    )
    for plan in plans:
        if plan.kind == "orphan":
            console.print(
                f"  [orphan] {plan.unit_name}  (disable+stop, remove unit + marker)"
            )
        else:
            console.print(f"  [stale_marker] {plan.marker_path.name}  ({plan.reason})")

    if dry_run:
        console.print(
            f"Would prune {orphan_count} orphan unit(s) "
            f"and {stale_count} stale marker(s)."
        )
        ctx.exit(EXIT_OK)

    if not yes:
        if not click.confirm(
            f"About to disable + remove {orphan_count} unit(s) and "
            f"{stale_count} stale marker(s). Proceed?",
            default=False,
        ):
            console.print("Aborted by operator.")
            ctx.exit(EXIT_OK)

    # Execute. Direct fs + systemctl invocations under operator-typed sudo.
    runner = LocalRunner()
    failures: list[str] = []
    for plan in plans:
        if plan.kind == "orphan" and plan.unit_name:
            if plan.is_timer:
                try:
                    runner.run(["systemctl", "disable", "--now", plan.unit_name])
                except OSError as exc:
                    failures.append(f"disable {plan.unit_name}: {exc}")
            else:
                try:
                    runner.run(["systemctl", "stop", plan.unit_name])
                except OSError as exc:
                    failures.append(f"stop {plan.unit_name}: {exc}")

        if plan.unit_path is not None:
            try:
                plan.unit_path.unlink(missing_ok=True)
            except OSError as exc:
                failures.append(f"unlink {plan.unit_path}: {exc}")
        try:
            plan.marker_path.unlink(missing_ok=True)
        except OSError as exc:
            failures.append(f"unlink {plan.marker_path}: {exc}")

    # Single daemon-reload after all removes.
    try:
        runner.run(["systemctl", "daemon-reload"])
    except OSError as exc:
        failures.append(f"daemon-reload: {exc}")

    if failures:
        for f in failures:
            err_console.print(f"[red]Error:[/red] {f}")
        ctx.exit(EXIT_OPERATOR_ERROR)
    console.print(
        f"Pruned {orphan_count} orphan unit(s) and {stale_count} stale marker(s)."
    )
    ctx.exit(EXIT_OK)


def _run_validate_only(
    diffs: list[UnitDiff],
    ctx: click.Context,
    *,
    verbose: bool,
) -> None:
    """--validate-only: classify only, exit 0/1/2 per state."""
    missing = [d for d in diffs if d.state is UnitState.MISSING_SOURCE]
    drifted = [d for d in diffs if d.state is UnitState.DRIFTED]

    if missing:
        err_console.print(
            f"[red]Error:[/red] source files missing for {len(missing)} unit(s):"
        )
        for d in missing:
            err_console.print(f"  - {d.install.unit_name} ({d.install.source_path})")
        ctx.exit(EXIT_OPERATOR_ERROR)

    if drifted:
        err_console.print(
            f"[yellow]Warning:[/yellow] {len(drifted)} unit(s) drifted "
            "(dest differs from source):"
        )
        for d in drifted:
            err_console.print(f"  - {d.install.unit_name}: {d.diff_summary}")
            if verbose:
                _print_verbose_diff(d)
        ctx.exit(EXIT_POLICY_VIOLATION)

    console.print(f"OK: {len(diffs)} unit(s) identical to source.")
    ctx.exit(EXIT_OK)


def _run_apply(
    diffs: list[UnitDiff],
    ctx: click.Context,
    *,
    force: bool,
    yes: bool,
    verbose: bool,
    via_socket: bool = False,
    socket_path: Path | None = None,
    config_path: Path | None = None,
) -> None:
    """Real apply path. Prompts unless ``yes``."""
    if verbose:
        for d in diffs:
            if d.state is UnitState.DRIFTED:
                _print_verbose_diff(d)

    work_count = sum(
        1
        for d in diffs
        if d.state is UnitState.ABSENT or (d.state is UnitState.DRIFTED and force)
    )
    if work_count == 0 and not any(
        d.state in (UnitState.MISSING_SOURCE, UnitState.DRIFTED) for d in diffs
    ):
        console.print(f"All {len(diffs)} unit(s) already in sync. Nothing to do.")
        ctx.exit(EXIT_OK)

    if not yes and work_count > 0:
        if not click.confirm(
            f"About to install {work_count} unit(s) into /etc/systemd/system/. "
            "Proceed?",
            default=False,
        ):
            console.print("Aborted by operator.")
            ctx.exit(EXIT_OK)

    if via_socket:
        if socket_path is None or not socket_path.is_socket():
            err_console.print(
                "[red]Error:[/red] unit-installer socket not found at "
                f"[bold]{socket_path}[/bold]. "
                "This host has not been bootstrapped with the v0.29 helper. "
                "Run [bold]fraisier scaffold-install --yes[/bold] first."
            )
            ctx.exit(EXIT_OPERATOR_ERROR)
        try:
            report = apply_unit_diffs_via_helper(
                diffs,
                socket_path=socket_path,
                force=force,
                write_markers=config_path is not None,
                config_path=config_path,
            )
        except ScheduledInstallError as exc:
            err_console.print(f"[red]Error:[/red] {exc}")
            ctx.exit(EXIT_OPERATOR_ERROR)
        if report.rejected_reason is not None:
            err_console.print(
                f"[red]Helper rejected manifest:[/red] {report.rejected_reason}"
            )
            ctx.exit(EXIT_POLICY_VIOLATION)
        console.print(
            f"Installed {len(report.written)} unit(s) via helper; "
            f"enabled {len(report.enabled_timers)} timer(s); "
            f"skipped {len(report.skipped_identical)} identical."
        )
        ctx.exit(EXIT_OK)

    try:
        report = apply_unit_diffs(diffs, runner=LocalRunner(), force=force)
    except ScheduledInstallError as exc:
        msg = str(exc)
        err_console.print(f"[red]Error:[/red] {msg}")
        if "drifted units" in msg:
            ctx.exit(EXIT_POLICY_VIOLATION)
        ctx.exit(EXIT_OPERATOR_ERROR)

    console.print(
        f"Installed {len(report.written)} unit(s); "
        f"enabled {len(report.enabled_timers)} timer(s); "
        f"skipped {len(report.skipped_identical)} identical."
    )
    ctx.exit(EXIT_OK)


def _resolve_socket_path(
    config: FraisierConfig, env: str, *, override: str | None
) -> Path:
    if override:
        return Path(override)
    # Third copy of this formula (#337): the socket unit's ListenStream= decides
    # where the socket is, and deployers/scheduled.py looks for it too. Both
    # consumers degrade quietly on absence, so a drift here does not crash.
    return Path(f"/run/fraisier/{env}/unit-installer-{config.project_name}.sock")

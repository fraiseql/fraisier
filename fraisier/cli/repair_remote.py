"""repair-remote command — apply fixes found by validate-remote."""

from __future__ import annotations

import json

import click
from rich.table import Table

from ._helpers import console, require_config, resolve_sudo_password
from .bootstrap import _resolve_server_and_runner
from .main import main


@main.command(name="repair-remote")
@click.argument("fraise")
@click.argument("environment")
@click.option(
    "--ssh-user",
    default=None,
    help="SSH user for initial connection (default: ~/.ssh/config or root)",
)
@click.option(
    "--ssh-port",
    default=None,
    type=int,
    help="SSH port (default: from ~/.ssh/config or 22)",
)
@click.option(
    "--ssh-key",
    default=None,
    type=click.Path(),
    help="Path to SSH private key (default: from ~/.ssh/config)",
)
@click.option(
    "--server",
    default=None,
    help="Target server hostname (overrides environments.<env>.server)",
)
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
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Apply fixes without confirmation prompt",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def repair_remote(
    ctx: click.Context,
    fraise: str,
    environment: str,
    ssh_user: str | None,
    ssh_port: int | None,
    ssh_key: str | None,
    server: str | None,
    sudo: bool,
    become_password_command: str | None,
    ask_become_pass: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Apply fixes found by validate-remote on the target server.

    Runs the same checks as validate-remote, then for each failed check
    that has a safe, deterministic fix, applies it remotely. Re-validates
    after all repairs to confirm what was fixed.

    Checks with auto-fix support:
      - app_path ownership / missing directory  →  chown / mkdir + chown
      - git_repo ownership                      →  chown -R
      - systemd service / socket not active     →  systemctl enable --now
      - wrapper script not executable           →  chmod 755

    Checks requiring manual intervention (no auto-fix):
      - SSH connectivity failure
      - Missing git repo                        →  fraisier bootstrap
      - Missing wrapper scripts / sudoers       →  fraisier scaffold-install

    \b
    Examples:
        fraisier repair-remote my_api production
        fraisier repair-remote my_api staging --yes
        fraisier repair-remote my_api production --ssh-user lionel --sudo
        fraisier repair-remote my_api production --ssh-user lionel -K
        fraisier repair-remote my_api production --become-password-command "op read op://…"
    """
    from fraisier.remote_validator import RemoteDeploymentValidator

    config = require_config(ctx)

    fraise_config = config.get_fraise_environment(fraise, environment)
    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    sudo, sudo_password = resolve_sudo_password(
        config, environment, become_password_command, sudo, ask_become_pass
    )

    target_server, runner = _resolve_server_and_runner(
        ctx,
        environment,
        ssh_user,
        ssh_port,
        ssh_key,
        server,
        sudo=sudo,
        sudo_password=sudo_password,
    )

    if not as_json:
        console.print(
            f"\n[bold]Remote Repair: {fraise} / {environment}[/bold]"
            f" on [cyan]{target_server}[/cyan]\n"
        )

    validator = RemoteDeploymentValidator(fraise_config, runner, config)

    if not as_json:
        with console.status("Checking remote state…"):
            initial_results = validator.run_all()
    else:
        initial_results = validator.run_all()

    fixable = [r for r in initial_results if not r.passed and r.fix_command is not None]
    manual = [
        r
        for r in initial_results
        if not r.passed and r.fix_command is None and r.name != "ssh_connectivity"
    ]

    if not fixable and not manual:
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "server": target_server,
                        "fraise": fraise,
                        "environment": environment,
                        "repairs": [],
                        "final_checks": [r.to_dict() for r in initial_results],
                    },
                    indent=2,
                )
            )
        else:
            console.print("[green]All checks passed — nothing to repair.[/green]\n")
        raise SystemExit(0)

    if not as_json:
        _print_plan(initial_results, fixable, manual)

        if not fixable:
            console.print(
                "[yellow]No auto-fixable issues found. "
                "See manual steps above.[/yellow]\n"
            )
            raise SystemExit(1)

        if not yes:
            click.confirm(
                f"Apply {len(fixable)} fix(es) on {target_server}?",
                default=True,
                abort=True,
            )
        console.print()
    elif not fixable:
        click.echo(
            json.dumps(
                {
                    "server": target_server,
                    "fraise": fraise,
                    "environment": environment,
                    "repairs": [],
                    "final_checks": [r.to_dict() for r in initial_results],
                },
                indent=2,
            )
        )
        raise SystemExit(1)

    if not as_json:
        with console.status("Applying fixes…"):
            repair_results = validator.repair_all(initial_results)
        with console.status("Re-checking remote state…"):
            final_results = validator.run_all()
    else:
        repair_results = validator.repair_all(initial_results)
        final_results = validator.run_all()

    if as_json:
        all_fixed = all(r.applied for r in repair_results) and all(
            r.passed for r in final_results if r.severity == "error"
        )
        click.echo(
            json.dumps(
                {
                    "server": target_server,
                    "fraise": fraise,
                    "environment": environment,
                    "repairs": [
                        {
                            "check_name": r.check_name,
                            "fix_command": r.fix_command,
                            "applied": r.applied,
                            "stdout": r.stdout,
                            "stderr": r.stderr,
                        }
                        for r in repair_results
                    ],
                    "final_checks": [r.to_dict() for r in final_results],
                },
                indent=2,
            )
        )
        raise SystemExit(0 if all_fixed else 1)

    _print_results(final_results, repair_results, manual)

    remaining_errors = any(
        not r.passed and r.severity == "error" for r in final_results
    )
    raise SystemExit(0 if not remaining_errors else 1)


def _print_plan(initial_results, fixable, manual) -> None:
    """Print the pre-repair plan table showing what will be fixed and what won't."""
    table = Table(show_header=False, show_lines=False)
    table.add_column("", style="dim", width=3)
    table.add_column("Check", style="cyan")
    table.add_column("Details", no_wrap=False)

    for r in initial_results:
        if r.passed:
            table.add_row("✓", r.name, f"[green]{r.message or 'OK'}[/green]")
        elif r.fix_command is not None:
            table.add_row(
                "~",
                r.name,
                f"[yellow]{r.message}[/yellow]\n"
                f"[dim]Will run: {r.fix_command}[/dim]",
            )
        elif r.name == "ssh_connectivity":
            table.add_row("✗", r.name, f"[red]{r.message or 'failed'}[/red]")
        else:
            table.add_row(
                "✗",
                r.name,
                f"[red]{r.message or 'failed'}[/red]\n"
                "[dim]Requires manual intervention[/dim]",
            )

    console.print(table)
    console.print()

    summary_parts = []
    if fixable:
        summary_parts.append(f"[yellow]{len(fixable)} auto-fixable[/yellow]")
    if manual:
        summary_parts.append(
            f"[red]{len(manual)} require{'s' if len(manual) == 1 else ''} "
            "manual intervention[/red]"
        )
    console.print(f"{', '.join(summary_parts)}.\n")


def _print_results(final_results, repair_results, manual) -> None:
    """Print the post-repair results table and summary."""
    table = Table(show_header=False, show_lines=False)
    table.add_column("", style="dim", width=3)
    table.add_column("Check", style="cyan")
    table.add_column("Result", no_wrap=False)

    repair_by_name = {r.check_name: r for r in repair_results}
    for result in final_results:
        repair = repair_by_name.get(result.name)
        if repair is not None:
            if repair.applied and result.passed:
                table.add_row("✓", result.name, "[green]Fixed[/green]")
            elif repair.applied and not result.passed:
                table.add_row(
                    "✗",
                    result.name,
                    f"[red]Fix applied but check still fails: {result.message}[/red]",
                )
            else:
                err = repair.stderr.strip() or "non-zero exit"
                table.add_row(
                    "✗",
                    result.name,
                    f"[red]Fix command failed: {err}[/red]",
                )
        elif result.passed:
            table.add_row("✓", result.name, f"[green]{result.message or 'OK'}[/green]")
        elif result.severity == "warning":
            table.add_row(
                "⚠", result.name, f"[yellow]{result.message or 'warning'}[/yellow]"
            )
        else:
            table.add_row("✗", result.name, f"[red]{result.message or 'failed'}[/red]")

    console.print(table)
    console.print()

    final_by_name = {r.name: r for r in final_results}
    fixed_confirmed = 0
    failed_repairs = 0
    still_broken = 0
    for rep in repair_results:
        if not rep.applied:
            failed_repairs += 1
        elif final_by_name.get(rep.check_name) and final_by_name[rep.check_name].passed:
            fixed_confirmed += 1
        else:
            still_broken += 1

    summary_parts = []
    if fixed_confirmed:
        summary_parts.append(f"[green]{fixed_confirmed} fixed[/green]")
    if failed_repairs:
        summary_parts.append(f"[red]{failed_repairs} fix failed[/red]")
    if still_broken:
        summary_parts.append(f"[red]{still_broken} still broken[/red]")
    if manual:
        summary_parts.append(f"[yellow]{len(manual)} need manual fix[/yellow]")

    remaining_errors = any(
        not r.passed and r.severity == "error" for r in final_results
    )
    status_color = "green" if not remaining_errors else "red"
    status_text = "READY" if not remaining_errors else "NOT READY"
    console.print(
        f"Summary: {', '.join(summary_parts)} → "
        f"[{status_color}]{status_text}[/{status_color}]\n"
    )

"""Scaffold command for generating infrastructure files."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal

import click

from fraisier.scaffold.sudoers_diff import SudoersDiff, diff_sudoers

from ._helpers import console, require_config
from .main import main

# Distinct exit code for --strict-sudoers aborts (#224). 1 stays generic;
# this lets CI/automation distinguish "sudoers would change" from any other
# install failure without parsing the output.
STRICT_SUDOERS_EXIT_CODE = 3


@main.command(name="scaffold-diff")
@click.argument("fraise", required=False)
@click.argument("environment", required=False)
@click.option("--apply", is_flag=True, help="Apply diffs (re-install changed files)")
@click.option("--server", "-s", default=None, help="Only include paths for this server")
@click.pass_context
def scaffold_diff(
    ctx: click.Context,
    fraise: str | None,
    environment: str | None,
    apply: bool,
    server: str | None,
) -> None:
    """Compare scaffold files against installed system files.

    Shows unified diffs for files that differ between the generated scaffold
    and what's currently installed on the system. Use --apply to automatically
    re-install changed files.

    \b
    Exit codes:
        0 - No differences found
        1 - Differences found

    \b
    Examples:
        fraisier scaffold-diff                    # all fraises/environments
        fraisier scaffold-diff api production    # specific fraise/env
        fraisier scaffold-diff --apply           # apply all differences
    """
    from fraisier.scaffold.diff import compute_scaffold_diff

    config = require_config(ctx)

    # Compute differences
    diffs = compute_scaffold_diff(
        config=config,
        server=server,
        fraise_filter=fraise,
        env_filter=environment,
    )

    if not diffs:
        console.print("[green]✓[/green] No scaffold differences found")
        raise SystemExit(0)

    # Display results
    changed_count = 0
    for diff in diffs:
        if diff.status == "match":
            console.print(f"[green]✓[/green] {diff.generated_path}")
        elif diff.status == "missing_installed":
            console.print(f"[red]✗[/red] {diff.generated_path} - missing from system")
            changed_count += 1
        elif diff.status == "missing_generated":
            console.print(f"[yellow]?[/yellow] {diff.generated_path} - not in scaffold")
        elif diff.status == "permission_denied":
            console.print(
                f"[yellow]![/yellow] {diff.generated_path}"
                " - permission denied (cannot compare)"
            )
        elif diff.status == "differs":
            console.print(f"[red]✗[/red] {diff.generated_path}")
            if diff.diff_lines:
                # Show first few lines of diff
                for line in diff.diff_lines[:10]:  # Limit output
                    console.print(f"  {line.rstrip()}")
                if len(diff.diff_lines) > 10:
                    console.print(f"  ... ({len(diff.diff_lines) - 10} more lines)")
            changed_count += 1

    # Summary
    total_files = len(diffs)
    console.print(f"\nSummary: {changed_count}/{total_files} files differ")

    if apply and changed_count > 0:
        from fraisier.scaffold.diff import apply_scaffold_diffs

        console.print("\n[cyan]Applying changes...[/cyan]")
        applied, failures = apply_scaffold_diffs(config, diffs, server=server)

        for path in applied:
            console.print(f"[green]✓[/green] Updated {path}")
        for path, error in failures:
            console.print(f"[red]✗[/red] Failed {path}: {error}")

        if failures:
            console.print(f"\n[red]{len(failures)} file(s) failed to apply.[/red]")
            raise SystemExit(1)

        console.print(f"\n[green]Applied {len(applied)} change(s).[/green]")
        raise SystemExit(0)

    # Exit with appropriate code
    raise SystemExit(1 if changed_count > 0 else 0)


@main.command(name="scaffold")
@click.option("--dry-run", is_flag=True, help="Show what would be generated")
@click.option(
    "--server",
    "-s",
    default=None,
    help="Only include paths for this server",
)
@click.pass_context
def scaffold(ctx: click.Context, dry_run: bool, server: str | None) -> None:
    """Generate infrastructure files from fraises.yaml.

    Renders systemd units, nginx configs, GitHub Actions workflows,
    sudoers, install scripts, confiture configs, and shell scripts.

    \b
    Examples:
        fraisier scaffold
        fraisier scaffold --dry-run
        fraisier scaffold --server server-1
    """
    from fraisier.scaffold.renderer import ScaffoldRenderer

    config = ctx.obj["config"]
    renderer = ScaffoldRenderer(config, server=server)
    files = renderer.render(dry_run=dry_run)

    if dry_run:
        console.print("[cyan]Would generate the following files:[/cyan]")
        for f in files:
            console.print(f"  {config.scaffold.output_dir}/{f}")
    else:
        console.print(
            f"[green]Generated {len(files)} files "
            f"in {config.scaffold.output_dir}[/green]"
        )
        for f in files:
            console.print(f"  {f}")

        # Provide helpful next steps
        console.print("\n[cyan]Next steps:[/cyan]")
        console.print("  1. Review generated files:")
        console.print(f"     git diff {config.scaffold.output_dir}/")
        console.print("\n  2. Install to system:")
        console.print("     fraisier scaffold-install --dry-run    # Preview")
        console.print("     fraisier scaffold-install --yes        # Install")


def _run_script(cmd: list[str]) -> int:
    """Run a script and return the exit code."""
    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except FileNotFoundError as e:
        console.print(
            "[red]Error:[/red] Could not run script. Please ensure sudo is available.",
            style="bold",
        )
        raise SystemExit(1) from e


def _build_preview_cmd(cmd: list[str]) -> list[str]:
    """Build a preview command by adding --dry-run flag."""
    if "--dry-run" in cmd:
        return cmd
    preview = list(cmd)
    # Insert before other flags
    flag_count = sum(1 for c in preview if c.startswith("--"))
    insert_pos = len(preview) - flag_count
    preview.insert(insert_pos, "--dry-run")
    return preview


def _print_install_failure(
    *,
    install_script: Path,
    returncode: int,
    cmd: list[str],
    phase: str | None,
) -> None:
    """Print the failure message for a non-zero install.sh exit.

    ``phase`` is ``"Validation"`` or ``"Preview"`` for the dry-run / validate
    paths, or ``None`` for a real install. The rerun hint adds ``--verbose``
    if it wasn't already present so the operator's copy-paste produces a
    diagnostic log even when the original invocation didn't.
    """
    rerun_flags = " ".join(cmd[2:])  # skip ["sudo", install_script]
    if "--verbose" not in rerun_flags:
        rerun_flags = f"{rerun_flags} --verbose".strip()
    if phase is not None:
        headline = f"[yellow]⚠ {phase} exited with code {returncode}.[/yellow]"
    else:
        headline = f"[red]✗ Installation failed (exit code {returncode}).[/red]"
    console.print(
        f"\n{headline}\n"
        "To capture the full output for debugging:\n"
        f"  sudo {install_script} {rerun_flags} 2>&1 | tee /tmp/install.log",
        soft_wrap=True,
    )


def _read_current_sudoers(
    project_name: str,
) -> tuple[str | None, Literal["ok", "missing", "unreadable"]]:
    """Read `/etc/sudoers.d/<project_name>` via sudo for the diff check (#224).

    Returns:
        ``(content, "ok")`` when the file was read successfully.
        ``(None, "missing")`` when the file does not exist (fresh install).
        ``(None, "unreadable")`` when sudo refused, the file was unreadable,
        or any other I/O error occurred.

    Uses plain ``sudo`` (not ``sudo -n``) so the read piggybacks on the
    existing scaffold-install sudo timestamp: interactive runs warm it via
    the preceding install.sh preview; ``--yes`` runs warm it via the
    install itself. ``sudo test -f`` distinguishes "missing" from
    "can't read" without paying a second password prompt.
    """
    target = f"/etc/sudoers.d/{project_name}"
    try:
        probe = subprocess.run(
            ["sudo", "test", "-f", target],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None, "unreadable"
    if probe.returncode == 1:
        return None, "missing"
    if probe.returncode != 0:
        return None, "unreadable"
    try:
        result = subprocess.run(
            ["sudo", "cat", target],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None, "unreadable"
    if result.returncode != 0:
        return None, "unreadable"
    return result.stdout, "ok"


def _print_sudoers_diff(
    *,
    sudoers_src: Path,
    project_name: str,
    strict: bool,
) -> SudoersDiff | None:
    """Print the sudoers-rule removal warning and return the diff (#224).

    Returns ``None`` if the check was skipped (no source file, no target on
    disk, or unreadable target in non-strict mode). Raises ``SystemExit(3)``
    in strict mode when the current sudoers can't be read.
    """
    if not sudoers_src.exists():
        return None
    content, status = _read_current_sudoers(project_name)
    if status == "missing":
        return None
    if status == "unreadable":
        if strict:
            console.print(
                "\n[red]✗ --strict-sudoers: could not read "
                f"/etc/sudoers.d/{project_name} to verify what would "
                "change.[/red]"
            )
            raise SystemExit(STRICT_SUDOERS_EXIT_CODE)
        console.print(
            f"\n[yellow]Note: could not read /etc/sudoers.d/{project_name}; "
            "skipping sudoers diff.[/yellow]"
        )
        return None
    assert content is not None  # status == "ok" implies content is set
    diff = diff_sudoers(content, sudoers_src.read_text())
    if diff.removed:
        console.print(
            f"\n[yellow]⚠ {len(diff.removed)} sudoers rule(s) currently in "
            f"/etc/sudoers.d/{project_name} are not in your fraises.yaml "
            "and would be removed:[/yellow]"
        )
        for rule in diff.removed:
            console.print(f"  - {rule}")
    return diff


@main.command(name="scaffold-install")
@click.option("--dry-run", is_flag=True, help="Preview what would be installed")
@click.option(
    "--validate-only", is_flag=True, help="Check prerequisites only (no install)"
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose output")
@click.option(
    "--strict-sudoers",
    is_flag=True,
    help=(
        "Abort (exit 3) if sudoers rules would be removed or current "
        "/etc/sudoers.d/<project> can't be read. Intended for CI/automation."
    ),
)
@click.pass_context
def scaffold_install(
    ctx: click.Context,
    dry_run: bool,
    validate_only: bool,
    yes: bool,
    verbose: bool,
    strict_sudoers: bool,
) -> None:
    """Install generated scaffold files to system locations.

    Runs the generated install.sh script with sudo to install systemd units,
    nginx configs, sudoers rules, wrapper scripts, and system dependencies.

    Must run 'fraisier scaffold' first to generate the files.

    Prerequisites:
    - Must run 'fraisier scaffold' first
    - Must have sudo access (or be running as root)
    - Generated files must be in PROJECT_DIR (usually /opt/<project_name>)

    \b
    Examples:
        fraisier scaffold-install --dry-run     # Preview changes
        fraisier scaffold-install --validate-only # Check prerequisites
        fraisier scaffold-install --yes          # Install without prompt
    """
    config = require_config(ctx)

    # Locate the install.sh script
    output_dir = Path(config.scaffold.output_dir)
    install_script = output_dir / "install.sh"

    # Path.exists() only swallows ENOENT/ENOTDIR/ELOOP/EBADF; EACCES on a
    # parent directory propagates as PermissionError. Treat any stat failure
    # as "can't see the file" so the friendly message wins over a traceback.
    try:
        exists = install_script.exists()
    except OSError:
        exists = False

    if not exists:
        console.print(
            f"[red]Error:[/red] {install_script} not found or not readable.\n"
            "Run 'fraisier scaffold' first to generate it.",
            style="bold",
        )
        raise SystemExit(1)

    try:
        is_file = install_script.is_file()
    except OSError:
        is_file = False

    if not is_file:
        console.print(
            f"[red]Error:[/red] {install_script} is not a regular file "
            "or is not readable.",
            style="bold",
        )
        raise SystemExit(1)

    # Make sure install.sh is executable. If we can't chmod (e.g. file owned
    # by another user), only fail when the file isn't already executable —
    # otherwise the chmod is a no-op anyway.
    try:
        install_script.chmod(0o755)
    except OSError as exc:
        if not os.access(install_script, os.X_OK):
            console.print(
                f"[red]Error:[/red] cannot make {install_script} executable: "
                f"{exc.strerror or exc}.\n"
                f"Fix permissions and retry, e.g.: "
                f"sudo chmod +x {install_script}",
                style="bold",
            )
            raise SystemExit(1) from exc

    # Build the command
    cmd: list[str] = ["sudo", str(install_script)]
    if dry_run:
        cmd.append("--dry-run")
    if validate_only:
        cmd.append("--validate-only")
    if verbose:
        cmd.append("--verbose")

    # Show what will happen
    if validate_only:
        console.print("[cyan]Checking prerequisites...[/cyan]\n")
    elif dry_run:
        console.print("[cyan]Preview of what would be installed:[/cyan]\n")
    else:
        console.print("[cyan]Installation plan:[/cyan]\n")

    sudoers_src = output_dir / "sudoers"

    def _check_sudoers_diff() -> None:
        """Print the sudoers diff and honor --strict-sudoers.

        Closure over `config.project_name`, `sudoers_src`, and `strict_sudoers`
        so the call sites below stay short. Raises SystemExit(3) when
        --strict-sudoers detects a removal or an unreadable target.
        """
        diff = _print_sudoers_diff(
            sudoers_src=sudoers_src,
            project_name=config.project_name,
            strict=strict_sudoers,
        )
        if strict_sudoers and diff is not None and diff.removed:
            console.print(
                "\n[red]✗ --strict-sudoers: aborting because sudoers rules "
                "would be removed. Add them to sudoers_rules in fraises.yaml "
                "or remove --strict-sudoers.[/red]"
            )
            raise SystemExit(STRICT_SUDOERS_EXIT_CODE)

    # If not --yes and not validating/dry-running, show preview first
    if not yes and not validate_only and not dry_run:
        preview_cmd = _build_preview_cmd(cmd)
        _run_script(preview_cmd)
        console.print()
        # Single prompt covers BOTH the install plan AND the sudoers diff:
        # chaining a second `click.confirm` here would train operators to mash
        # `y` past safety questions.
        _check_sudoers_diff()
        if not click.confirm("Proceed with installation?"):
            console.print("[yellow]Aborted.[/yellow]")
            return
    else:
        # --yes / --dry-run / --validate-only: still surface the diff (loudly
        # in --yes, as part of the preview output otherwise). The operator
        # opted out of the prompt, not out of seeing what would change.
        _check_sudoers_diff()

    # Run the actual command
    returncode = _run_script(cmd)

    if returncode == 0:
        if validate_only:
            console.print("\n[green]✓ All prerequisites met![/green]")
        elif dry_run:
            console.print("\n[green]✓ Preview complete[/green]")
        else:
            console.print(
                "\n[green]✓ Installation complete![/green]\n"
                "[cyan]Next steps:[/cyan]\n"
                "  1. Enable and start socket units:\n"
                "     sudo systemctl enable fraisier-{project}-*-deploy.socket\n"
                "     sudo systemctl start fraisier-{project}-*-deploy.socket\n"
                "  2. Verify socket units are listening:\n"
                "     systemctl status fraisier-{project}-*-deploy.socket\n"
                "  3. Test deployment:\n"
                "     fraisier trigger-deploy <fraise> <environment>"
            )
    else:
        if validate_only:
            phase: str | None = "Validation"
        elif dry_run:
            phase = "Preview"
        else:
            phase = None
        _print_install_failure(
            install_script=install_script,
            returncode=returncode,
            cmd=cmd,
            phase=phase,
        )
        raise SystemExit(returncode)

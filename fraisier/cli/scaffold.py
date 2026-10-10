"""Scaffold command for generating infrastructure files."""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import click

from fraisier.scaffold.sudoers_diff import SudoersDiff, diff_sudoers

from ._helpers import console, require_config
from .main import main

if TYPE_CHECKING:
    from fraisier.root_policy import RootPolicy

# Distinct exit code for --strict-sudoers aborts (#224). 1 stays generic;
# this lets CI/automation distinguish "sudoers would change" from any other
# install failure without parsing the output.
STRICT_SUDOERS_EXIT_CODE = 3


def _report_foreign_units(config, *, server: str | None = None) -> None:
    """Name the units installed here that belong to a fraise running elsewhere.

    A report, never an action. Removing another application's service as a
    side effect of a diagnostic is how a routine command becomes an outage
    (#336, decision 5), so this says what is there and who owns it and
    leaves the operator to decide.
    """
    from fraisier.scaffold import foreign as foreign_mod

    try:
        units = foreign_mod.find_foreign_units(config, server=server)
    except (OSError, ValueError) as exc:
        console.print(f"[yellow]![/yellow] could not check for foreign units: {exc}")
        return

    if not units:
        return

    console.print(
        f"\n[yellow]![/yellow] {len(units)} foreign unit(s) — installed here, "
        "owned by a fraise that does not run on this host:"
    )
    for unit in units:
        console.print(
            f"    {unit.unit_name}  <- owned by {unit.owner}, "
            f"installed at {unit.installed_path}"
        )
    console.print(
        "  Nothing was stopped or removed. To disable and delete them:\n"
        "    fraisier scaffold-install --prune-foreign"
    )


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

    _report_foreign_units(config, server=server)

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


def _prune_foreign_units(config, *, yes: bool) -> None:
    """Disable and delete this host's foreign units, on explicit request.

    Reached only through ``--prune-foreign``: this is the one path in the
    scoping fix that stops a systemd unit, and the unit it stops may be
    another application's. Typed, listed, and confirmed unless ``--yes``.
    """
    from fraisier.scaffold import foreign as foreign_mod

    units = foreign_mod.find_foreign_units(config)
    if not units:
        console.print("[green]✓[/green] No foreign units to prune")
        return

    console.print(
        f"[yellow]![/yellow] --prune-foreign will disable and delete "
        f"{len(units)} unit(s):"
    )
    for unit in units:
        console.print(f"    {unit.unit_name}  <- owned by {unit.owner}")

    if not yes and not click.confirm("Disable and delete these units?"):
        console.print("[yellow]Left in place.[/yellow]")
        return

    pruned = foreign_mod.prune_foreign_units(config, units)
    console.print(f"[green]✓[/green] Pruned {len(pruned)} foreign unit(s)")


@main.command(name="scaffold")
@click.option("--dry-run", is_flag=True, help="Show what would be generated")
@click.option(
    "--server",
    "-s",
    default=None,
    help="Only include paths for this server",
)
@click.option(
    "--output-dir",
    "output_dir",
    default=None,
    help=(
        "Render into this directory instead of scaffold.output_dir. Used by the "
        "deploy path to materialize the server-side scaffold state tree."
    ),
)
@click.pass_context
def scaffold(
    ctx: click.Context, dry_run: bool, server: str | None, output_dir: str | None
) -> None:
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
    if output_dir:
        renderer.output_dir = Path(output_dir)
    files = renderer.render(dry_run=dry_run)
    dest_dir = str(renderer.output_dir)

    if dry_run:
        console.print("[cyan]Would generate the following files:[/cyan]")
        for f in files:
            console.print(f"  {dest_dir}/{f}")
    else:
        console.print(f"[green]Generated {len(files)} files in {dest_dir}[/green]")
        for f in files:
            console.print(f"  {f}")

        # Provide helpful next steps
        console.print("\n[cyan]Next steps:[/cyan]")
        console.print("  1. Review generated files:")
        console.print(f"     git diff {config.scaffold.output_dir}/")
        console.print("\n  2. Install to system:")
        console.print("     fraisier scaffold-install --dry-run    # Preview")
        console.print("     fraisier scaffold-install --yes        # Install")


def _build_install_cmd(
    install_script: str,
    *,
    dry_run: bool,
    validate_only: bool,
    verbose: bool,
) -> list[str]:
    """Build the ``sudo install.sh`` argv.

    ``FRAISIER_DEPLOY_IN_FLIGHT`` is re-stated as ``--deploy-in-flight`` because
    this is the one hop where the environment does not survive: a deploy sets
    the variable, this process inherits it, and ``sudo`` resets it before
    ``install.sh`` ever sees it. Without the flag, install.sh would restart the
    webhook that is running the deploy invoking it (#349).
    """
    cmd: list[str] = ["sudo", install_script]
    if dry_run:
        cmd.append("--dry-run")
    if validate_only:
        cmd.append("--validate-only")
    if verbose:
        cmd.append("--verbose")
    if os.environ.get("FRAISIER_DEPLOY_IN_FLIGHT"):
        cmd.append("--deploy-in-flight")
    return cmd


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
    returncode: int,
    rerun_flags: list[str],
    phase: str | None,
) -> None:
    """Print the failure message for a non-zero install.sh exit.

    ``phase`` is ``"Validation"`` or ``"Preview"`` for the dry-run / validate
    paths, or ``None`` for a real install. The rerun hint is this command, not
    the install.sh it ran: that script was rendered into a private directory
    that is gone by now. ``--verbose`` is added if it wasn't already present so
    the operator's copy-paste produces a diagnostic log even when the original
    invocation didn't.
    """
    flags = [*rerun_flags, *([] if "--verbose" in rerun_flags else ["--verbose"])]
    if phase is not None:
        headline = (
            f"[yellow]⚠ {phase} failed: install.sh exited with code "
            f"{returncode}.[/yellow]"
        )
    else:
        headline = (
            f"[red]✗ Installation failed (install.sh exited with code "
            f"{returncode}).[/red]"
        )
    console.print(
        f"\n{headline}\n"
        "To capture the full output for debugging:\n"
        f"  sudo fraisier scaffold-install {' '.join(flags)} 2>&1 "
        "| tee /tmp/install.log",
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


# ---------------------------------------------------------------------------
# The operator's root install (#433, path 8)
# ---------------------------------------------------------------------------


def _euid() -> int:
    return os.geteuid()


def _hostname() -> str:
    from fraisier.scaffold_install_helper import short_hostname

    return short_hostname()


def _read_installed(path: str) -> bytes | None:
    from fraisier.scaffold_apply import read_installed

    return read_installed(path)


def _read_current_policy(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError, UnicodeDecodeError:
        return None


def _write_root_policy(policy: RootPolicy) -> None:
    """Write *policy* where only root can, atomically, mode 0644."""
    from fraisier.root_policy import dump_policy, policy_path

    path = policy_path(policy.project)
    for directory in (path.parent.parent, path.parent):
        directory.mkdir(mode=0o755, exist_ok=True)
        os.chown(directory, 0, 0)
        directory.chmod(0o755)
    tmp = path.with_name(f".{path.name}.new")
    tmp.write_text(dump_policy(policy))
    os.chown(tmp, 0, 0)
    tmp.chmod(0o644)
    tmp.replace(path)


def _render_root_tree(config) -> Path:
    """Render *config* as root into a fresh directory only root can write.

    Never the tree a deploy rendered: the deploy user writes that one, and
    installing it as root is the escalation #433 closes.
    """
    from fraisier.scaffold.renderer import ScaffoldRenderer

    tree = Path(tempfile.mkdtemp(prefix=f"fraisier-{config.project_name}-scaffold-"))
    renderer = ScaffoldRenderer(config)
    renderer.output_dir = tree
    renderer.render()
    # `sudo` runs it directly, and the renderer writes it 0644.
    (tree / "install.sh").chmod(0o700)
    return tree


def _host_payload(tree: Path) -> dict:
    from fraisier.scaffold.artifacts import ARTIFACT_MANIFEST_NAME

    return json.loads((tree / ARTIFACT_MANIFEST_NAME).read_text())


def _print_root_file_diffs(tree: Path, chosen: list[dict]) -> None:
    """Every root-owned file this install would write, as a diff of what is there."""
    changed = 0
    console.print("\n[cyan]Root-owned files this install writes:[/cyan]")
    for artifact in sorted(chosen, key=lambda a: a["destination"]):
        dest = artifact["destination"]
        rendered = (tree / artifact["source"]).read_bytes()
        installed = _read_installed(dest)
        if installed == rendered:
            continue
        changed += 1
        if installed is None:
            console.print(f"  new: {dest}", markup=False)
            continue
        diff = difflib.unified_diff(
            installed.decode(errors="replace").splitlines(),
            rendered.decode(errors="replace").splitlines(),
            fromfile=f"{dest} (installed)",
            tofile=f"{dest} (rendered)",
            lineterm="",
        )
        console.print("\n".join(diff), markup=False, highlight=False)
    if not changed:
        console.print("  (none differ from what is installed)")


def _app_unit_texts(payload: dict, hostname: str) -> dict[str, str]:
    """The app's own units this host's unit-installer copies, as they are now."""
    from fraisier.scaffold.artifacts import host_gate_open
    from fraisier.scaffold_apply import SafeTree, UnsafeTreeError

    host = (payload.get("hosts") or {}).get(hostname) or {}
    texts: dict[str, str] = {}
    for unit in payload.get("app_managed") or []:
        if not host_gate_open(unit, host):
            continue
        try:
            data = SafeTree(unit["source_dir"]).read(unit["unit_name"])
        except UnsafeTreeError as exc:
            console.print(f"  [yellow]skipped {unit['unit_name']}: {exc}[/yellow]")
            continue
        if data is not None:
            texts[unit["unit_name"]] = data.decode(errors="replace")
    return texts


def _exec_trees(config) -> list[str]:
    """Directories a non-root unit may run anything under (#433)."""
    trees = {
        f"/home/{config.scaffold.deploy_user}/.local/bin/",
        f"{config.scaffold_state_dir}/",
    }
    for fraise in config.fraises.values():
        for env in (fraise.get("environments") or {}).values():
            if isinstance(env, dict) and env.get("app_path"):
                trees.add(f"{str(env['app_path']).rstrip('/')}/")
    return sorted(trees)


def _draft_root_policy(config, tree: Path, payload: dict, hostname: str):
    from fraisier.root_policy_build import PolicyBuildError, build_policy

    try:
        return build_policy(
            project=config.project_name,
            scaffold_dir=str(config.scaffold_state_dir),
            payload=payload,
            hostname=hostname,
            read_source=lambda rel: (
                (tree / rel).read_text() if (tree / rel).is_file() else None
            ),
            app_units=_app_unit_texts(payload, hostname),
            trees=_exec_trees(config),
        )
    except PolicyBuildError as exc:
        console.print(
            f"\n[yellow]No root policy for this host: {exc}.[/yellow]", markup=False
        )
        return None


def _print_policy_diff(draft) -> None:
    from fraisier.root_policy import dump_policy, policy_path

    path = policy_path(draft.policy.project)
    current = _read_current_policy(path) or ""
    new = dump_policy(draft.policy)
    console.print(f"\n[cyan]Root policy ({path}):[/cyan]")
    if current == new:
        console.print("  (unchanged)")
    else:
        diff = difflib.unified_diff(
            current.splitlines(),
            new.splitlines(),
            fromfile=f"{path} (installed)",
            tofile=f"{path} (new)",
            lineterm="",
        )
        console.print("\n".join(diff), markup=False, highlight=False)
    for note in draft.notes:
        console.print(f"  note: {note}", markup=False)


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
@click.option(
    "--output-dir",
    "output_dir_opt",
    default=None,
    help=(
        "Refused: scaffold-install renders its own tree as root, and never "
        "installs one someone else rendered (#433)."
    ),
)
@click.option(
    "--prune-foreign",
    is_flag=True,
    help=(
        "Disable and delete units installed here that belong to a fraise "
        "which does not run on this host. Never happens by default: they may "
        "be another application's running services. Run 'fraisier "
        "scaffold-diff' first to see what would go."
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
    output_dir_opt: str | None,
    prune_foreign: bool,
) -> None:
    """Render the scaffold as root and install it, after showing what changes.

    Run it as root: `sudo fraisier scaffold-install`. It renders fraises.yaml
    into a private directory, shows a diff of every root-owned file it would
    write and of the root policy it would grant deploys, asks, runs that
    render's install.sh, and then writes the root policy
    (/etc/fraisier/<project>/root-policy.json) that bounds what a deploy may
    install as root (#433). `--yes` skips the question, not the diff.

    \b
    Examples (each under sudo):
        fraisier scaffold-install --dry-run       # Preview changes
        fraisier scaffold-install --validate-only # Check prerequisites
        fraisier scaffold-install --yes           # Install without prompt
    """
    config = require_config(ctx)

    if _euid() != 0:
        console.print(
            "[red]Error:[/red] scaffold-install writes root-owned files and must "
            "render them as root. Run: sudo fraisier scaffold-install",
            style="bold",
        )
        raise SystemExit(1)
    if output_dir_opt:
        console.print(
            "[red]Error:[/red] --output-dir is refused: scaffold-install renders "
            "its own tree as root and never installs one someone else rendered "
            "(#433).",
            style="bold",
        )
        raise SystemExit(1)

    if prune_foreign and not (dry_run or validate_only):
        _prune_foreign_units(config, yes=yes)

    tree = _render_root_tree(config)
    try:
        _install_root_tree(
            config,
            tree,
            dry_run=dry_run,
            validate_only=validate_only,
            yes=yes,
            verbose=verbose,
            strict_sudoers=strict_sudoers,
        )
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def _install_root_tree(
    config,
    tree: Path,
    *,
    dry_run: bool,
    validate_only: bool,
    yes: bool,
    verbose: bool,
    strict_sudoers: bool,
) -> None:
    from fraisier.scaffold.artifacts import host_artifacts

    install_script = tree / "install.sh"
    cmd = _build_install_cmd(
        str(install_script),
        dry_run=dry_run,
        validate_only=validate_only,
        verbose=verbose,
    )

    # Show what will happen
    if validate_only:
        console.print("[cyan]Checking prerequisites...[/cyan]\n")
    elif dry_run:
        console.print("[cyan]Preview of what would be installed:[/cyan]\n")
    else:
        console.print("[cyan]Installation plan:[/cyan]\n")

    sudoers_src = tree / "sudoers"
    hostname = _hostname()
    payload = _host_payload(tree)
    chosen = host_artifacts(payload, hostname)
    draft = _draft_root_policy(config, tree, payload, hostname)

    def _show_changes() -> None:
        """Every root-owned file and the policy, then the sudoers check.

        Printed before anything runs, `--yes` included: the operator opted
        out of the question, not out of seeing the answer (#433, path 8).
        """
        if chosen is not None:
            _print_root_file_diffs(tree, chosen)
        if draft is not None:
            _print_policy_diff(draft)
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
        _run_script(_build_preview_cmd(cmd))
        console.print()
        # Single prompt covers the install plan, the root-owned diffs, the
        # policy and the sudoers diff: chaining a second `click.confirm` here
        # would train operators to mash `y` past safety questions.
        _show_changes()
        if not click.confirm("Proceed with installation?"):
            console.print("[yellow]Aborted.[/yellow]")
            return
    else:
        _show_changes()

    # Run the actual command
    returncode = _run_script(cmd)

    if returncode == 0:
        if validate_only:
            console.print("\n[green]✓ All prerequisites met![/green]")
        elif dry_run:
            console.print("\n[green]✓ Preview complete[/green]")
        else:
            if draft is not None:
                _write_root_policy(draft.policy)
                console.print("\n[green]✓ Root policy written.[/green]")
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
        rerun = [
            flag
            for flag, on in (
                ("--yes", yes),
                ("--dry-run", dry_run),
                ("--validate-only", validate_only),
                ("--verbose", verbose),
            )
            if on
        ]
        _print_install_failure(returncode=returncode, rerun_flags=rerun, phase=phase)
        raise SystemExit(returncode)

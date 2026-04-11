"""Setup validation CLI command and its helpers."""

from __future__ import annotations

import os
from pathlib import Path

import click

from ._helpers import console
from .main import main


@main.command()
@click.argument("fraise")
@click.argument("environment")
@click.option("--json", is_flag=True, help="Output validation results in JSON format")
@click.pass_context
def validate_setup(
    ctx: click.Context, fraise: str, environment: str, json: bool
) -> None:
    """Validate socket activation setup for a fraise.

    Checks systemd version, socket paths, permissions, and unit files
    to ensure socket activation is properly configured.

    \b
    Examples:
        fraisier validate-setup my_api development
        fraisier validate-setup my_api production --json
    """
    import json as json_module

    config = ctx.obj["config"]

    # Find the fraise configuration
    fraise_config = config.get_fraise(fraise)
    if not fraise_config:
        console.print(f"[red]Error:[/red] Fraise '{fraise}' not found")
        raise SystemExit(1)

    all_environments = fraise_config.get("environments", {})
    if environment not in all_environments:
        console.print(
            f"[red]Error:[/red] Environment '{environment}' not found"
            f" for fraise '{fraise}'"
        )
        raise SystemExit(1)

    from fraisier.naming import deploy_socket_name

    validation_results = {}

    # Check systemd version
    systemd_ok, systemd_version, systemd_msg = _check_systemd_version()
    validation_results["systemd"] = {
        "ok": systemd_ok,
        "version": systemd_version,
        "message": systemd_msg,
    }

    env_config = all_environments[environment]
    socket_unit = deploy_socket_name(env_config, environment, fraise)
    socket_stem = socket_unit.removesuffix(".socket")
    socket_dir = Path("/run/fraisier") / socket_stem
    socket_path = socket_dir / "deploy.sock"
    env_results = {
        environment: {
            "socket_directory": _check_socket_directory(socket_dir),
            "socket_file": _check_socket_file(socket_path),
            "socket_permissions": _check_socket_permissions(socket_path),
            "systemd_units": _check_systemd_units(socket_unit),
            "user_permissions": _check_user_permissions(socket_path),
        }
    }

    validation_results["environments"] = env_results

    # Overall status
    all_ok = systemd_ok and all(
        all(check["ok"] for check in env_checks.values())
        for env_checks in env_results.values()
    )

    if json:
        # JSON output
        output = {
            "fraise": fraise,
            "overall_status": "ok" if all_ok else "issues_found",
            **validation_results,
        }
        import sys

        json_module.dump(output, sys.stdout, indent=2)
        print()
    else:
        # Human-readable output
        console.print(f"[bold]Validating socket activation setup for '{fraise}'[/bold]")
        console.print()

        # Systemd check
        status_icon = "✓" if systemd_ok else "✗"
        color = "green" if systemd_ok else "red"
        console.print(f"[{color}]Systemd: {systemd_msg} {status_icon}[/{color}]")

        # Environment checks
        for env, checks in env_results.items():
            console.print(f"[bold]Environment: {env}[/bold]")

            for check_name, result in checks.items():
                status_icon = "✓" if result["ok"] else "✗"
                color = "green" if result["ok"] else "red"
                msg = f"{check_name}: {result['message']} {status_icon}"
                console.print(f"  [{color}]{msg}[/{color}]")

        console.print()
        if all_ok:
            console.print("[green]✓ All validation checks passed![/green]")
        else:
            console.print(
                "[yellow]⚠ Some validation checks failed. "
                "Run 'fraisier diagnose' for troubleshooting.[/yellow]"
            )
            raise SystemExit(1)


def _check_systemd_version() -> tuple[bool, str, str]:
    """Check if systemd version meets requirements (>= 230)."""
    try:
        import subprocess

        result = subprocess.run(
            ["systemctl", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            # First line: "systemd 249 (249.7-1-arch)"
            first_line = result.stdout.split("\n")[0]
            version_str = first_line.split()[1]  # Extract version number
            try:
                version = int(version_str.split(".")[0])  # Major version
                if version >= 230:
                    return True, version_str, f"systemd {version_str} (compatible)"
                else:
                    return (
                        False,
                        version_str,
                        f"systemd {version_str} (requires >= 230)",
                    )
            except ValueError:
                return (
                    False,
                    version_str,
                    f"systemd {version_str} (unable to parse version)",
                )
        else:
            return False, "unknown", "systemctl command failed"
    except (subprocess.SubprocessError, FileNotFoundError):
        return False, "unknown", "systemd not available"


def _check_socket_directory(socket_dir: Path) -> dict:
    """Check if socket directory exists and has correct permissions."""
    if not socket_dir.exists():
        return {"ok": False, "message": f"Directory {socket_dir} does not exist"}

    # Check permissions (should be 755 or similar)
    try:
        stat = socket_dir.stat()
        mode = stat.st_mode & 0o777
        if mode >= 0o755:  # Owner can read/write/execute, group/others can read/execute
            return {
                "ok": True,
                "message": f"Directory exists with permissions {oct(mode)}",
            }
        else:
            return {
                "ok": False,
                "message": f"Directory permissions {oct(mode)} too restrictive",
            }
    except OSError as e:
        return {"ok": False, "message": f"Cannot check directory permissions: {e}"}


def _check_socket_file(socket_path: Path) -> dict:
    """Check if socket file exists."""
    if socket_path.exists():
        return {"ok": True, "message": f"Socket file exists at {socket_path}"}
    else:
        return {"ok": False, "message": f"Socket file does not exist at {socket_path}"}


def _check_socket_permissions(socket_path: Path) -> dict:
    """Check socket file permissions."""
    if not socket_path.exists():
        return {"ok": False, "message": "Socket file does not exist"}

    try:
        stat = socket_path.stat()
        mode = stat.st_mode & 0o777

        # Socket files should be accessible to web group
        # Typically 660 (owner and group can read/write)
        if mode >= 0o660:
            return {"ok": True, "message": f"Socket has permissions {oct(mode)}"}
        else:
            return {
                "ok": False,
                "message": f"Socket permissions {oct(mode)} too restrictive",
            }
    except OSError as e:
        return {"ok": False, "message": f"Cannot check socket permissions: {e}"}


def _check_systemd_units(unit_name: str) -> dict:
    """Check if systemd units are installed and enabled."""

    try:
        import subprocess

        # Check if unit file exists
        result = subprocess.run(
            ["systemctl", "cat", unit_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return {"ok": False, "message": f"Systemd unit {unit_name} not found"}

        # Check if unit is enabled
        result = subprocess.run(
            ["systemctl", "is-enabled", unit_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and "enabled" in result.stdout:
            return {
                "ok": True,
                "message": f"Systemd unit {unit_name} is installed and enabled",
            }
        else:
            return {"ok": False, "message": f"Systemd unit {unit_name} not enabled"}

    except (subprocess.SubprocessError, FileNotFoundError):
        return {"ok": False, "message": "systemctl command not available"}


def _check_user_permissions(socket_path: Path) -> dict:
    """Check if current user can access the socket."""
    if not socket_path.exists():
        return {"ok": False, "message": "Socket file does not exist"}

    try:
        # Try to get socket file info
        stat = socket_path.stat()
        import grp
        import pwd

        # Get current user
        current_uid = pwd.getpwuid(os.getuid()).pw_uid
        current_user = pwd.getpwuid(os.getuid()).pw_name

        # Get socket owner/group
        socket_uid = stat.st_uid
        socket_gid = stat.st_gid

        # Check if user is owner or in group
        user_groups = [g.gr_gid for g in grp.getgrall() if current_user in g.gr_mem]
        user_groups.append(os.getgid())  # Primary group

        if current_uid == socket_uid or socket_gid in user_groups:
            return {"ok": True, "message": f"User {current_user} can access socket"}
        else:
            socket_group = grp.getgrgid(socket_gid).gr_name
            return {
                "ok": False,
                "message": f"User {current_user} not in socket group '{socket_group}'",
            }

    except (OSError, KeyError) as e:
        return {"ok": False, "message": f"Cannot check user permissions: {e}"}

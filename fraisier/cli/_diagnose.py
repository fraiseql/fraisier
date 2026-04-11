"""Deployment diagnose CLI command and its helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click

from ._helpers import console
from .main import main

if TYPE_CHECKING:
    import builtins


@main.command()
@click.argument("fraise")
@click.argument("environment")
@click.option("--json", is_flag=True, help="Output diagnostic results in JSON format")
@click.pass_context
def diagnose(ctx: click.Context, fraise: str, environment: str, json: bool) -> None:
    """Diagnose deployment issues for a fraise environment."""
    _diagnose(ctx, fraise, environment, json)


def _diagnose(ctx: click.Context, fraise: str, environment: str, json: bool) -> None:
    """Diagnose deployment issues for a fraise environment.

    Analyzes recent deployment logs, status files, and socket connectivity
    to identify issues and provide actionable troubleshooting steps.

    \b
    Examples:
        fraisier diagnose my_api production
        fraisier diagnose my_api development --json
    """
    config = ctx.obj["config"]

    fraise_config = config.get_fraise_environment(fraise, environment)
    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    from fraisier.naming import deploy_socket_name

    project_name = config.project_name
    run_dir = Path("/run/fraisier")
    socket_unit = deploy_socket_name(fraise_config, environment, fraise)
    socket_stem = socket_unit.removesuffix(".socket")
    socket_path = run_dir / socket_stem / "deploy.sock"
    status_path = run_dir / f"{project_name}-{environment}.last_deployment"
    service_name = fraise_config.get("systemd_service")

    socket_check = _diagnose_socket_connectivity(socket_path)
    status_check = _diagnose_deployment_status(status_path)
    systemd_check = _diagnose_systemd_service(service_name)
    socket_unit_check = _diagnose_systemd_socket_unit(socket_unit)

    issues_found, suggestions = _build_diagnostic_issues(
        socket_check,
        status_check,
        systemd_check,
        socket_unit_check,
        service_name,
        socket_unit,
        socket_path,
        ctx,
        fraise,
    )

    diagnostic_results = {
        "socket_connectivity": socket_check,
        "recent_deployment": status_check,
        "systemd_service": systemd_check,
        "socket_unit": socket_unit_check,
        "issues_found": issues_found,
        "suggestions": suggestions,
    }

    _output_diagnose_results(
        json,
        fraise,
        environment,
        diagnostic_results,
        status_check,
        issues_found,
        suggestions,
    )


def _build_diagnostic_issues(
    socket_check: dict,
    status_check: dict,
    systemd_check: dict,
    socket_unit_check: dict,
    service_name: str,
    socket_unit: str,
    socket_path: Path,
    ctx: click.Context,
    fraise: str,
) -> tuple[builtins.list[str], builtins.list[dict]]:
    issues_found: builtins.list[str] = []
    suggestions: builtins.list[dict] = []

    if not socket_check["can_connect"]:
        issues_found.append("socket_connectivity")
        if socket_check["socket_exists"]:
            suggestions.append(
                {
                    "issue": "Socket exists but cannot connect",
                    "fixes": [
                        f"Check socket permissions: ls -la {socket_path}",
                        f"Verify user is in socket group: groups "
                        f"{ctx.obj.get('user', 'current_user')}",
                        f"Check systemd socket status: systemctl status {socket_unit}",
                        f"Restart socket unit: sudo systemctl restart {socket_unit}",
                    ],
                }
            )
        else:
            suggestions.append(
                {
                    "issue": "Socket file does not exist",
                    "fixes": [
                        f"Enable socket unit: sudo systemctl enable {socket_unit}",
                        f"Start socket unit: sudo systemctl start {socket_unit}",
                        f"Check socket unit file: systemctl cat {socket_unit}",
                    ],
                }
            )

    if status_check["status"] == "failed":
        issues_found.append("recent_deployment")
        error_msg = status_check.get("error", "Unknown error")
        suggestions.append(
            {
                "issue": f"Recent deployment failed: {error_msg}",
                "fixes": [
                    f"Check deployment logs: journalctl -u {service_name} -n 50",
                    "Verify app configuration in fraises.yaml",
                    f"Test service manually: sudo systemctl start {service_name}",
                    f"Check app logs in /opt/{fraise}/logs/",
                ],
            }
        )

    if not systemd_check["service_exists"]:
        issues_found.append("systemd_service")
        suggestions.append(
            {
                "issue": f"Systemd service {service_name} not found",
                "fixes": [
                    f"Install service unit: sudo cp "
                    f"scripts/generated/systemd/{service_name} /etc/systemd/system/",
                    "Reload systemd: sudo systemctl daemon-reload",
                    f"Enable service: sudo systemctl enable {service_name}",
                ],
            }
        )
    elif not systemd_check["service_running"]:
        issues_found.append("systemd_service")
        suggestions.append(
            {
                "issue": f"Systemd service {service_name} not running",
                "fixes": [
                    f"Check service status: systemctl status {service_name}",
                    f"Start service: sudo systemctl start {service_name}",
                    f"Check service logs: journalctl -u {service_name} -n 20",
                ],
            }
        )

    if not socket_unit_check["unit_exists"]:
        issues_found.append("socket_unit")
        suggestions.append(
            {
                "issue": f"Socket unit {socket_unit} not found",
                "fixes": [
                    f"Install socket unit: sudo cp "
                    f"scripts/generated/systemd/{socket_unit} /etc/systemd/system/",
                    "Reload systemd: sudo systemctl daemon-reload",
                    f"Enable socket: sudo systemctl enable {socket_unit}",
                ],
            }
        )
    elif not socket_unit_check["unit_active"]:
        issues_found.append("socket_unit")
        suggestions.append(
            {
                "issue": f"Socket unit {socket_unit} not active",
                "fixes": [
                    f"Check socket status: systemctl status {socket_unit}",
                    f"Start socket: sudo systemctl start {socket_unit}",
                    f"Check socket logs: journalctl -u {socket_unit} -n 20",
                ],
            }
        )

    return issues_found, suggestions


def _output_diagnose_results(
    json_flag: bool,
    fraise: str,
    environment: str,
    diagnostic_results: dict,
    status_check: dict,
    issues_found: builtins.list[str],
    suggestions: builtins.list[dict],
) -> None:
    if json_flag:
        import json as json_module
        import sys

        output = {
            "fraise": fraise,
            "environment": environment,
            "diagnostics": diagnostic_results,
        }
        json_module.dump(output, sys.stdout, indent=2)
        print()
        return

    console.print(f"[bold]Diagnosing issues for '{fraise}' / '{environment}'[/bold]")
    console.print()

    if not issues_found:
        console.print("[green]✓ No deployment issues detected[/green]")
        console.print()
        console.print("Recent deployment status:")
        if status_check["status"]:
            console.print(f"  Status: {status_check['status']}")
            if status_check.get("deployed_version"):
                console.print(f"  Version: {status_check['deployed_version']}")
            if status_check.get("deployed_at"):
                console.print(f"  Deployed: {status_check['deployed_at']}")
        else:
            console.print("  No recent deployments found")
    else:
        console.print(f"[red]⚠ Found {len(issues_found)} potential issue(s):[/red]")
        console.print()

        for i, suggestion in enumerate(suggestions, 1):
            console.print(f"[bold]{i}. {suggestion['issue']}[/bold]")
            console.print("   Suggested fixes:")
            for fix in suggestion["fixes"]:
                console.print(f"     • {fix}")
            console.print()

        console.print(
            "[yellow]Run 'fraisier validate-setup' to check prerequisites.[/yellow]"
        )


def _diagnose_socket_connectivity(socket_path: Path) -> dict:
    """Test if socket is accepting connections."""
    result: dict[str, object] = {
        "socket_exists": socket_path.exists(),
        "can_connect": False,
        "error": None,
    }

    if not socket_path.exists():
        return result

    try:
        import socket as socket_module

        sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(str(socket_path))
        sock.close()
        result["can_connect"] = True
    except (OSError, ConnectionRefusedError) as e:
        result["error"] = str(e)

    return result


def _diagnose_deployment_status(status_path: Path) -> dict:
    """Analyze recent deployment status."""
    result: dict[str, object] = {
        "status_file_exists": status_path.exists(),
        "status": None,
        "deployed_version": None,
        "deployed_at": None,
        "error": None,
    }

    if not status_path.exists():
        return result

    try:
        import json as json_module

        data = json_module.loads(status_path.read_text())
        result.update(
            {
                "status": data.get("status"),
                "deployed_version": data.get("deployed_version"),
                "deployed_at": data.get("deployed_at"),
                "error": data.get("error"),
            }
        )
    except (OSError, json_module.JSONDecodeError) as e:
        result["error"] = f"Cannot read status file: {e}"

    return result


def _diagnose_systemd_service(service_name: str) -> dict:
    """Check systemd service status."""
    if not service_name:
        return {"service_name": None, "service_exists": False, "service_running": False}

    result = {
        "service_name": service_name,
        "service_exists": False,
        "service_running": False,
        "error": None,
    }

    try:
        import subprocess

        # Check if service exists
        check_result = subprocess.run(
            ["systemctl", "cat", service_name],
            capture_output=True,
            timeout=5,
            check=False,
        )
        result["service_exists"] = check_result.returncode == 0

        if result["service_exists"]:
            # Check if service is running
            status_result = subprocess.run(
                ["systemctl", "is-active", service_name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            result["service_running"] = "active" in status_result.stdout

    except (subprocess.SubprocessError, FileNotFoundError) as e:
        result["error"] = str(e)

    return result


def _diagnose_systemd_socket_unit(unit_name: str) -> dict:
    """Check systemd socket unit status."""
    result = {
        "unit_name": unit_name,
        "unit_exists": False,
        "unit_active": False,
        "error": None,
    }

    try:
        import subprocess

        # Check if unit exists
        check_result = subprocess.run(
            ["systemctl", "cat", unit_name], capture_output=True, timeout=5, check=False
        )
        result["unit_exists"] = check_result.returncode == 0

        if result["unit_exists"]:
            # Check if unit is active
            status_result = subprocess.run(
                ["systemctl", "is-active", unit_name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            result["unit_active"] = "active" in status_result.stdout

    except (subprocess.SubprocessError, FileNotFoundError) as e:
        result["error"] = str(e)

    return result

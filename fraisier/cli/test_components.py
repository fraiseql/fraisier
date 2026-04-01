"""Test individual deployment components in isolation."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import click
from rich.table import Table

from ._helpers import _get_deployer, console, require_config
from .main import main


def _validate_wrapper_executable(wrapper_path: str) -> Path:
    """Validate wrapper script exists and is executable."""
    wrapper_path_obj = Path(wrapper_path)
    if not wrapper_path_obj.exists():
        console.print(f"[red]Error:[/red] Wrapper script not found at {wrapper_path}")
        raise SystemExit(1)
    if not os.access(wrapper_path, os.X_OK):
        console.print(
            f"[red]Error:[/red] Wrapper script not executable: {wrapper_path}"
        )
        raise SystemExit(1)
    return wrapper_path_obj


def _get_wrapper_path(wrapper_type: str) -> str:
    """Get wrapper script path from environment variable."""
    wrapper_env_map = {
        "systemctl": "FRAISIER_SYSTEMCTL_WRAPPER",
        "pg": "FRAISIER_PG_WRAPPER",
        "psql": "FRAISIER_PG_WRAPPER",  # Allow both pg and psql
    }

    if wrapper_type not in wrapper_env_map:
        console.print(
            f"[red]Error:[/red] Unknown wrapper type '{wrapper_type}'. "
            f"Supported types: {', '.join(wrapper_env_map.keys())}"
        )
        raise SystemExit(1)

    env_var = wrapper_env_map[wrapper_type]
    wrapper_path = os.environ.get(env_var)

    if not wrapper_path:
        console.print(
            f"[red]Error:[/red] {env_var} environment variable not set. "
            f"Wrapper '{wrapper_type}' not configured."
        )
        raise SystemExit(1)

    return wrapper_path


@main.command(name="test-wrapper")
@click.argument("fraise")
@click.argument("environment")
@click.argument("args", nargs=-1, required=True)
@click.pass_context
def test_wrapper(
    ctx: click.Context, fraise: str, environment: str, args: tuple[str, ...]
) -> None:
    """Test wrapper script execution.

    Validates and runs a wrapper script with the given arguments.
    Wrapper type is determined from the first argument (systemctl or pg).

    \b
    Examples:
        fraisier test-wrapper api development systemctl restart api.service
        fraisier test-wrapper api development pg version
    """
    config = require_config(ctx)
    fraise_config = config.get_fraise_environment(fraise, environment)

    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    if not args:
        console.print("[red]Error:[/red] No wrapper command specified")
        raise SystemExit(1)

    wrapper_type = args[0]
    wrapper_args = args[1:]
    wrapper_path = _get_wrapper_path(wrapper_type)
    _validate_wrapper_executable(wrapper_path)

    console.print(f"[bold]Testing wrapper:[/bold] {wrapper_type}")
    console.print(f"[bold]Script:[/bold] {wrapper_path}")
    console.print(f"[bold]Arguments:[/bold] {' '.join(wrapper_args)}")
    console.print()

    # Run wrapper script
    start_time = time.time()
    try:
        result = subprocess.run(
            [wrapper_path, *wrapper_args],
            check=False,
        )
        duration = time.time() - start_time

        console.print()
        console.print(f"[bold]Exit code:[/bold] {result.returncode}")
        console.print(f"[bold]Duration:[/bold] {duration:.2f}s")

        if result.returncode == 0:
            console.print("[green]✓ Wrapper test successful[/green]")
        else:
            console.print(
                f"[red]✗ Wrapper test failed (exit code {result.returncode})[/red]"
            )

        raise SystemExit(result.returncode)

    except SystemExit:
        raise
    except Exception as e:
        console.print(f"[red]Error running wrapper:[/red] {e}")
        raise SystemExit(1) from e


def _check_install_configured(deployer, fraise_type: str) -> None:
    """Validate install step is configured."""
    if not hasattr(deployer, "_install_dependencies"):
        console.print(
            f"[red]Error:[/red] Fraise type '{fraise_type}' does not support "
            "install testing"
        )
        raise SystemExit(1)

    if not deployer.install_command:
        console.print("[yellow]No install command configured for this fraise[/yellow]")
        raise SystemExit(1)

    if not deployer.app_path:
        console.print("[red]Error:[/red] No app_path configured")
        raise SystemExit(1)


def _display_install_error(e: Exception, duration: float) -> None:
    """Display install error with context."""
    from fraisier.errors import DeploymentError

    console.print()
    console.print(f"[bold]Duration:[/bold] {duration:.2f}s")
    console.print("[red]✗ Install step failed[/red]")
    console.print()

    if isinstance(e, DeploymentError) and hasattr(e, "context") and e.context:
        console.print("[bold]Error Details:[/bold]")
        for key, value in e.context.items():
            if key in ("command", "cwd", "exit_code", "suggested_command"):
                console.print(f"  {key}: {value}")
            elif key == "stderr" and value:
                console.print(f"  stderr: {value[:500]}")

    console.print()
    console.print(f"[red]{e}[/red]")


@main.command(name="test-install")
@click.argument("fraise")
@click.argument("environment")
@click.pass_context
def test_install(ctx: click.Context, fraise: str, environment: str) -> None:
    """Test the install step for a fraise/environment.

    Executes the configured install command in isolation without
    performing a full deployment.

    \b
    Examples:
        fraisier test-install api development
        fraisier test-install api production
    """
    from fraisier.errors import DeploymentError

    config = require_config(ctx)
    fraise_config = config.get_fraise_environment(fraise, environment)

    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    fraise_type = fraise_config.get("type")
    deployer = _get_deployer(fraise_type, fraise_config)

    if deployer is None:
        console.print(f"[red]Error:[/red] Unknown fraise type '{fraise_type}'")
        raise SystemExit(1)

    _check_install_configured(deployer, fraise_type)

    console.print(f"[bold]Testing install step:[/bold] {fraise} / {environment}")
    console.print(f"[bold]Working directory:[/bold] {deployer.app_path}")
    console.print(f"[bold]Command:[/bold] {' '.join(deployer.install_command)}")
    console.print()

    start_time = time.time()
    try:
        deployer._install_dependencies()
        duration = time.time() - start_time

        console.print()
        console.print(f"[bold]Duration:[/bold] {duration:.2f}s")
        console.print("[green]✓ Install step successful[/green]")

    except DeploymentError as e:
        duration = time.time() - start_time
        _display_install_error(e, duration)
        raise SystemExit(1) from e

    except Exception as e:
        duration = time.time() - start_time
        console.print()
        console.print(f"[bold]Duration:[/bold] {duration:.2f}s")
        console.print(f"[red]✗ Install step error: {e}[/red]")
        raise SystemExit(1) from e


@main.command(name="test-health")
@click.argument("fraise")
@click.argument("environment")
@click.pass_context
def test_health(ctx: click.Context, fraise: str, environment: str) -> None:
    """Test the health check for a fraise/environment.

    Executes the configured health check endpoint and reports the result.

    \b
    Examples:
        fraisier test-health api development
        fraisier test-health api production
    """
    config = require_config(ctx)
    fraise_config = config.get_fraise_environment(fraise, environment)

    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    fraise_type = fraise_config.get("type")
    deployer = _get_deployer(fraise_type, fraise_config)

    if deployer is None:
        console.print(f"[red]Error:[/red] Unknown fraise type '{fraise_type}'")
        raise SystemExit(1)

    # Check health check is configured
    if not hasattr(deployer, "health_check_url"):
        console.print(
            f"[red]Error:[/red] Fraise type '{fraise_type}' does not support "
            "health checks"
        )
        raise SystemExit(1)

    if not deployer.health_check_url:
        console.print(
            "[yellow]No health check endpoint configured for this fraise[/yellow]"
        )
        raise SystemExit(1)

    console.print(f"[bold]Testing health check:[/bold] {fraise} / {environment}")
    console.print(f"[bold]URL:[/bold] {deployer.health_check_url}")
    console.print(f"[bold]Timeout:[/bold] {deployer.health_check_timeout}s")
    console.print(f"[bold]Retries:[/bold] {deployer.health_check_retries}")
    console.print()

    start_time = time.time()
    try:
        # Single-shot health check (no retries) for immediate feedback
        ok = deployer.health_check()
        duration = time.time() - start_time

        console.print(f"[bold]Duration:[/bold] {duration:.2f}s")

        if ok:
            console.print("[green]✓ Health check passed[/green]")
        else:
            console.print("[red]✗ Health check failed[/red]")
            raise SystemExit(1)

    except SystemExit:
        raise
    except Exception as e:
        duration = time.time() - start_time
        console.print(f"[bold]Duration:[/bold] {duration:.2f}s")
        console.print(f"[red]✗ Health check error: {e}[/red]")
        raise SystemExit(1) from e


def _add_git_check_rows(
    deployer, table: Table
) -> tuple[bool, bool, bool, bool, bool, bool, bool, bool]:
    """Add git check rows to table. Returns tuple of check results."""
    # Check 1: Clone URL
    clone_ok = bool(deployer.clone_url)
    if clone_ok:
        table.add_row("✓", "Clone URL", f"[green]{deployer.clone_url}[/green]")
    else:
        table.add_row("✗", "Clone URL", "[yellow]Not configured[/yellow]")

    # Check 2: Bare repo path
    bare_repo_str = str(deployer.bare_repo)
    table.add_row("→", "Bare repo path", f"[dim]{bare_repo_str}[/dim]")

    # Check 3: Bare repo exists
    repo_exists = deployer.bare_repo.exists()
    if repo_exists:
        table.add_row("✓", "Bare repo exists", "[green]Yes[/green]")
    else:
        table.add_row("✗", "Bare repo exists", "[red]No[/red]")

    # Check 4: App path
    app_ok = False
    if deployer.app_path:
        app_path = Path(deployer.app_path)
        if app_path.exists():
            table.add_row("✓", "App path exists", f"[green]{deployer.app_path}[/green]")
            app_ok = True
        else:
            table.add_row("✗", "App path exists", f"[red]{deployer.app_path}[/red]")
    else:
        table.add_row("✗", "App path configured", "[red]No[/red]")

    # Check 5: Branch
    table.add_row("→", "Branch", f"[dim]{deployer.branch}[/dim]")

    # Check 6: Remote connectivity
    remote_ok = _check_remote_connectivity(deployer, table)

    # Check 7: Current version
    current_ok = _add_current_version_row(deployer, table)

    # Check 8: Latest version
    latest_ok = _add_latest_version_row(deployer, table)

    return clone_ok, repo_exists, app_ok, remote_ok, current_ok, latest_ok, False, False


def _check_remote_connectivity(deployer, table: Table) -> bool:
    """Check remote connectivity via git fetch."""
    if not (deployer.bare_repo.exists() and deployer.clone_url):
        return True

    try:
        result = subprocess.run(
            ["git", "-C", str(deployer.bare_repo), "fetch", "--dry-run"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            table.add_row(
                "✓",
                "Remote connectivity",
                "[green]Can fetch from remote[/green]",
            )
            return True
        table.add_row("✗", "Remote connectivity", "[red]Fetch failed[/red]")
        return False
    except subprocess.TimeoutExpired:
        table.add_row("✗", "Remote connectivity", "[red]Timeout[/red]")
        return False
    except Exception as e:
        table.add_row("✗", "Remote connectivity", f"[red]{e}[/red]")
        return False


def _add_current_version_row(deployer, table: Table) -> bool:
    """Add current version row to table."""
    try:
        current = deployer.get_current_version()
        if current:
            table.add_row("✓", "Current version", f"[green]{current}[/green]")
            return True
        table.add_row("·", "Current version", "[dim]Not deployed yet[/dim]")
        return True
    except Exception as e:
        table.add_row("✗", "Current version", f"[red]{e}[/red]")
        return False


def _add_latest_version_row(deployer, table: Table) -> bool:
    """Add latest version row to table."""
    try:
        latest = deployer.get_latest_version()
        if latest:
            table.add_row("✓", "Latest version", f"[green]{latest}[/green]")
            return True
        table.add_row("✗", "Latest version", "[red]Could not fetch[/red]")
        return False
    except Exception as e:
        table.add_row("✗", "Latest version", f"[red]{e}[/red]")
        return False


def _build_git_checks_table(deployer) -> tuple[Table, bool]:
    """Build git checks table and return (table, all_ok)."""
    table = Table(show_header=False, show_lines=False)
    table.add_column("", style="dim", width=3)
    table.add_column("Check", style="cyan")
    table.add_column("Status", no_wrap=False)

    check_results = _add_git_check_rows(deployer, table)
    all_ok = all(check_results)
    return table, all_ok


@main.command(name="test-git")
@click.argument("fraise")
@click.argument("environment")
@click.pass_context
def test_git(ctx: click.Context, fraise: str, environment: str) -> None:
    """Test git operations for a fraise/environment.

    Checks repository configuration, connectivity, and version info.

    \b
    Examples:
        fraisier test-git api development
        fraisier test-git api production
    """
    config = require_config(ctx)
    fraise_config = config.get_fraise_environment(fraise, environment)

    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    fraise_type = fraise_config.get("type")
    deployer = _get_deployer(fraise_type, fraise_config)

    if deployer is None:
        console.print(f"[red]Error:[/red] Unknown fraise type '{fraise_type}'")
        raise SystemExit(1)

    # Check if deployer supports git (has GitDeployMixin)
    if not hasattr(deployer, "bare_repo"):
        console.print(
            f"[red]Error:[/red] Fraise type '{fraise_type}' does not support "
            "git testing"
        )
        raise SystemExit(1)

    console.print(f"[bold]Testing git operations:[/bold] {fraise} / {environment}")
    console.print()

    table, all_ok = _build_git_checks_table(deployer)
    console.print(table)
    console.print()

    if all_ok:
        console.print("[green]✓ All git checks passed[/green]")
    else:
        console.print("[red]✗ Some git checks failed[/red]")
        raise SystemExit(1)


@main.command(name="test-database")
@click.argument("fraise")
@click.argument("environment")
@click.pass_context
def test_database(ctx: click.Context, fraise: str, environment: str) -> None:
    """Test database connectivity for a fraise/environment.

    Attempts to connect to the configured database and run a simple query.

    \b
    Examples:
        fraisier test-database api development
        fraisier test-database api production
    """
    config = require_config(ctx)
    fraise_config = config.get_fraise_environment(fraise, environment)

    if not fraise_config:
        console.print(
            f"[red]Error:[/red] Fraise '{fraise}' environment '{environment}' not found"
        )
        raise SystemExit(1)

    fraise_type = fraise_config.get("type")
    deployer = _get_deployer(fraise_type, fraise_config)

    if deployer is None:
        console.print(f"[red]Error:[/red] Unknown fraise type '{fraise_type}'")
        raise SystemExit(1)

    # Check if deployer has database config
    if not hasattr(deployer, "database_config"):
        console.print(
            f"[red]Error:[/red] Fraise type '{fraise_type}' does not support "
            "database testing"
        )
        raise SystemExit(1)

    if not deployer.database_config:
        console.print("[yellow]No database configuration for this fraise[/yellow]")
        raise SystemExit(1)

    database_url = deployer.database_config.get("database_url")
    if not database_url:
        console.print("[red]Error:[/red] No database_url configured")
        raise SystemExit(1)

    # Mask the password in display
    display_url = database_url.replace(
        database_url.split("@")[0] if "@" in database_url else database_url,
        "***masked***",
    )

    console.print(f"[bold]Testing database connection:[/bold] {fraise} / {environment}")
    console.print(f"[bold]URL:[/bold] {display_url}")
    console.print(
        f"[bold]Strategy:[/bold] {deployer.database_config.get('strategy', 'apply')}"
    )
    console.print()

    start_time = time.time()
    try:
        import psycopg2

        # Attempt connection
        conn = psycopg2.connect(database_url)
        cursor = conn.cursor()

        # Run simple query
        cursor.execute("SELECT 1")
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        duration = time.time() - start_time

        console.print(f"[bold]Connection time:[/bold] {duration * 1000:.1f}ms")
        console.print(f"[bold]Query result:[/bold] {result}")
        console.print("[green]✓ Database connection successful[/green]")

    except ImportError as e:
        console.print("[red]Error:[/red] psycopg2 not installed")
        console.print("[yellow]Install with:[/yellow] uv add psycopg2-binary")
        raise SystemExit(1) from e

    except Exception as e:
        duration = time.time() - start_time
        console.print(f"[bold]Duration:[/bold] {duration:.2f}s")
        console.print(f"[red]✗ Database connection failed: {e}[/red]")
        raise SystemExit(1) from e

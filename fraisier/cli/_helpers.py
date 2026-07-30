"""Shared utilities for CLI commands."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import click
from rich.console import Console

if TYPE_CHECKING:
    from fraisier.config import FraisierConfig


class _LazyConsole:
    """Lazy-initialized Rich Console with OutputMode dispatch.

    Delays Console construction until the first call so that a
    ``--no-color`` CLI flag (or the ``NO_COLOR`` environment variable)
    can be set *before* any output is produced.  All attribute and method
    accesses are transparently delegated to the underlying Console.

    ``print`` is the hot path; it consults the active
    :class:`fraisier._output.OutputContext`:

    - **Compact** (default): plain string args are routed through
      :func:`fraisier._output._strip_markup` and written to
      stdout/stderr without Rich rendering. Rich objects (Panel, Table,
      Syntax, …) fall through to the Rich console — Rich already
      strips ANSI when not on a TTY, so the output is clean for LLM
      and CI consumers.
    - **Verbose** (``-v``): today's Rich-markup output verbatim.
    - **JSON** (``--json``): suppressed entirely; structured events are
      emitted via :func:`fraisier._output.success` /
      :func:`fraisier._output.failure` + :func:`fraisier._output.emit_json`.
    """

    def __init__(self, *, stderr: bool = False) -> None:
        self._stderr = stderr
        self._console: Console | None = None

    def _get(self) -> Console:
        if self._console is None:
            self._console = Console(
                stderr=self._stderr,
                no_color=bool(os.environ.get("NO_COLOR")),
            )
        return self._console

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)

    def print(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        # Local import: avoids circular dep with fraisier.cli.main during
        # bootstrap (main.py imports from _helpers; _output imports click).
        from fraisier._output import OutputMode, _strip_markup, get_context

        ctx = get_context()
        if ctx.mode is OutputMode.JSON:
            return
        if ctx.mode is OutputMode.VERBOSE:
            self._get().print(*args, **kwargs)
            return
        # Compact mode: simple string args bypass Rich entirely; complex
        # Rich objects fall through (Rich strips ANSI for non-tty). Honour
        # ``markup=False`` (callers passing literal ``[...]`` content rely
        # on it; see scheduled-install's ``[would copy]`` lines).
        if len(args) == 1 and isinstance(args[0], str):
            import sys

            stream = sys.stderr if self._stderr else sys.stdout
            text = args[0] if kwargs.get("markup") is False else _strip_markup(args[0])
            stream.write(text + "\n")
            return
        self._get().print(*args, **kwargs)


# stdout console — respects NO_COLOR env var and --no-color flag
console: Any = _LazyConsole()
# stderr console — for error messages; keeps stdout/stderr cleanly separated
err_console: Any = _LazyConsole(stderr=True)


def parse_since(value: str) -> str:
    """Parse relative time or date string into ISO datetime.

    Args:
        value: Time string like "7d", "24h", "1h", or ISO date "2026-04-01"

    Returns:
        ISO datetime string

    Raises:
        ValueError: If the format is invalid
    """
    if not value:
        return ""

    # Check for relative time patterns
    relative_pattern = re.match(r"^(\d+)([dh])$", value)
    if relative_pattern:
        amount, unit = relative_pattern.groups()
        amount = int(amount)
        if unit == "d":
            delta = timedelta(days=amount)
        elif unit == "h":
            delta = timedelta(hours=amount)
        else:
            raise ValueError(f"Invalid time unit: {unit}")

        target_time = datetime.now() - delta
        return target_time.isoformat()

    # Check if it's already an ISO date (YYYY-MM-DD)
    try:
        parsed = datetime.fromisoformat(value)
        # If it's just a date, convert to start of that day
        if "T" not in value:
            parsed = datetime.combine(parsed.date(), datetime.min.time())
        return parsed.isoformat()
    except ValueError as err:
        raise ValueError(
            f"Invalid date/time format: {value}. Use '7d', '24h', or 'YYYY-MM-DD'"
        ) from err


def resolve_sudo_password(
    config: FraisierConfig,
    environment: str,
    become_password_command: str | None,
    sudo: bool,
    ask_become_pass: bool,
) -> tuple[bool, str | None]:
    """Resolve sudo password from CLI arg or config, prompt if needed.

    Resolution order:
      1. CLI --become-password-command
      2. bootstrap.environments.<env>.become_password_command
      3. bootstrap.servers.<server>.become_password_command
      4. bootstrap.become_password_command (global)
      5. Interactive prompt if --ask-become-pass

    Returns:
        (sudo, sudo_password) — sudo may be forced True when a password is found.
    """
    from fraisier.bootstrap import resolve_become_password

    if become_password_command is None:
        raw_bootstrap = config._config.get("bootstrap", {}) or {}

        env_override = (raw_bootstrap.get("environments") or {}).get(environment) or {}
        become_password_command = env_override.get("become_password_command")

        if become_password_command is None:
            env_cfg = config.environments.get(environment)
            server_name = env_cfg.get("server") if isinstance(env_cfg, dict) else None
            if server_name:
                servers = raw_bootstrap.get("servers") or {}
                srv_override = servers.get(server_name) or {}
                become_password_command = srv_override.get("become_password_command")

        if become_password_command is None:
            become_password_command = raw_bootstrap.get("become_password_command")

    if become_password_command:
        return True, resolve_become_password(become_password_command)
    if ask_become_pass:
        return True, click.prompt("SUDO password", hide_input=True, err=True)
    return sudo, None


def require_config(ctx: click.Context) -> FraisierConfig:
    """Get config from context, aborting with a clear error if missing."""
    config = ctx.obj.get("config")
    if config is None:
        err_console.print(
            "[red]Error:[/red] No fraises.yaml found. "
            "Run [bold]fraisier init[/bold] to create one, "
            "or use [bold]--config[/bold] to specify a path."
        )
        raise SystemExit(2)
    return config


def _print_dry_run(
    config: FraisierConfig,
    fraise: str,
    environment: str,
    fraise_config: dict,
) -> None:
    """Print a detailed dry-run deployment plan."""
    from rich.panel import Panel
    from rich.table import Table

    fraise_type = fraise_config.get("type", "unknown")
    strategy = (
        fraise_config.get("database", {}).get("strategy")
        or config.deployment.get_strategy(environment)
        or "basic"
    )
    from fraisier.naming import resolve_systemd_service

    db = fraise_config.get("database")
    hc = fraise_config.get("health_check")
    service = resolve_systemd_service(fraise_config)
    app_path = fraise_config.get("app_path", "")

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Step", style="bold cyan", min_width=16)
    table.add_column("Details")

    table.add_row("Target", f"{fraise} -> {environment}")
    table.add_row("Type", fraise_type)
    table.add_row("Strategy", strategy)
    if app_path:
        table.add_row("App path", app_path)

    # Database / backup / migration
    if db:
        db_name = db.get("name", "unknown")
        db_strategy = db.get("strategy", "none")
        if db.get("backup_before_deploy"):
            table.add_row("Backup", f"confiture preflight on {db_name}")
        if (db.get("pre_migrate_dump") or {}).get("enabled"):
            table.add_row(
                "Dump gate",
                f"verified pg_dump of {db_name} before migrate (abort on failure)",
            )
        table.add_row(
            "Migration",
            f"confiture migrate up on {db_name} (strategy: {db_strategy})",
        )
    else:
        table.add_row("Database", "none (no database configured)")

    # Service restart
    if service:
        table.add_row("Restart", service)

    # Health check
    if hc:
        url = hc.get("url", "")
        timeout = hc.get("timeout", 30)
        table.add_row("Health check", f"{url} (timeout: {timeout}s)")
    else:
        table.add_row("Health check", "none (skipped)")

    console.print(Panel(table, title="[cyan]DRY RUN[/cyan]", expand=False))


def _get_deployer(fraise_type: str | None, fraise_config: dict, job: str | None = None):
    """Get appropriate deployer for fraise type.

    When the fraise_config contains an ``ssh`` key, the deployer is
    configured with an ``SSHRunner`` so that commands execute on the
    remote host.  Otherwise a local ``LocalRunner`` is used.
    """
    from fraisier.runners import runner_from_config

    runner = runner_from_config(fraise_config.get("ssh"))

    if fraise_type == "api":
        from fraisier.deployers.api import APIDeployer

        return APIDeployer(fraise_config, runner=runner)

    elif fraise_type == "etl":
        from fraisier.deployers.etl import ETLDeployer

        return ETLDeployer(fraise_config, runner=runner)

    elif fraise_type == "docker_compose":
        from fraisier.deployers.docker_compose import DockerComposeDeployer

        return DockerComposeDeployer(fraise_config, runner=runner)

    elif fraise_type in ("scheduled", "backup"):
        from fraisier.deployers.scheduled import ScheduledDeployer

        # Handle nested jobs
        if job and "jobs" in fraise_config:
            job_config = fraise_config["jobs"].get(job)
            if job_config:
                return ScheduledDeployer(
                    {
                        **fraise_config,
                        **job_config,
                        "job_name": job,
                    },
                    runner=runner,
                )
        return ScheduledDeployer(fraise_config, runner=runner)

    return None

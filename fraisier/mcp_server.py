"""Fraisier MCP server — exposes bootstrap tools with elicitation support."""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel

from fraisier.bootstrap import ServerBootstrapper, resolve_become_password
from fraisier.config import FraisierConfig
from fraisier.preflight import PreflightChecker
from fraisier.runners import SSHRunner
from fraisier.ssh_config import resolve_ssh_config

mcp = FastMCP("fraisier")


class SudoPasswordSchema(BaseModel):
    sudo_password: str


def _load_config() -> FraisierConfig:
    """Load fraisier config from FRAISIER_CONFIG env var or default search."""
    config_path = os.environ.get("FRAISIER_CONFIG")
    if config_path:
        return FraisierConfig(Path(config_path))
    return FraisierConfig()


def _resolve_runner(
    config: FraisierConfig,
    environment: str,
    *,
    use_sudo: bool = False,
    sudo_password: str | None = None,
    ssh_user: str | None = None,
) -> tuple[str, SSHRunner]:
    """Resolve server and build SSHRunner from config."""
    env_cfg = config.environments.get(environment)
    server = env_cfg.get("server") if isinstance(env_cfg, dict) else None
    if not server:
        msg = f"environments.{environment}.server not set in config"
        raise ValueError(msg)

    host_config = resolve_ssh_config(server)
    runner = SSHRunner(
        host=server,
        user=ssh_user or host_config.user or "root",
        port=host_config.port or 22,
        key_path=host_config.identity_file,
        use_sudo=use_sudo,
        sudo_password=sudo_password,
    )
    return server, runner


@mcp.tool()
async def bootstrap_preflight(
    environment: str,
    ssh_user: str | None = None,
) -> str:
    """Run preflight checks on target server before bootstrap.

    Validates SSH connectivity, sudo access, required packages, disk space,
    and other prerequisites. Does not make any changes to the server.
    """
    config = _load_config()
    server, runner = _resolve_runner(config, environment, ssh_user=ssh_user)

    checker = PreflightChecker(runner=runner, deploy_user=config.scaffold.deploy_user)
    result = checker.run_all()

    lines = [f"Preflight checks for {environment} on {server}:"]
    for check in result.checks:
        symbol = "PASS" if check.passed else "FAIL"
        line = f"  [{symbol}] {check.name}"
        if check.message:
            line += f" ({check.message})"
        if not check.passed and check.fix_hint:
            line += f" — fix: {check.fix_hint}"
        lines.append(line)

    if result.passed:
        lines.append("\nAll checks passed. Ready for bootstrap.")
    else:
        n = result.failed_count
        lines.append(f"\n{n} check(s) failed. Fix issues above first.")

    return "\n".join(lines)


@mcp.tool()
async def bootstrap_server(
    environment: str,
    sudo: bool = False,
    ssh_user: str | None = None,
    dry_run: bool = False,
    ctx: Context | None = None,
) -> str:
    """Bootstrap a server end-to-end via SSH.

    Runs 10 ordered, idempotent provisioning steps. If sudo=True,
    prompts for the sudo password via elicitation (password is not
    stored in the conversation).
    """
    config = _load_config()
    sudo_password = None

    if sudo:
        # Resolution: config command > MCP elicitation
        raw_bootstrap = config._config.get("bootstrap", {}) or {}
        cmd = raw_bootstrap.get("become_password_command")
        if cmd:
            sudo_password = resolve_become_password(cmd)
        elif ctx is not None:
            result = await ctx.elicit(
                message="Enter the sudo password for the target server",
                schema=SudoPasswordSchema,
            )
            if result.action != "accept":
                return "Bootstrap aborted: sudo password was declined."
            sudo_password = result.data.sudo_password

    server, runner = _resolve_runner(
        config,
        environment,
        use_sudo=sudo,
        sudo_password=sudo_password,
        ssh_user=ssh_user,
    )

    bootstrapper = ServerBootstrapper(
        config=config,
        environment=environment,
        runner=runner,
        fraises_yaml_path=Path(config.config_path),
        dry_run=dry_run,
    )

    bootstrap_result = bootstrapper.bootstrap()

    lines = [f"Bootstrap {environment} on {server}:"]
    for i, step in enumerate(bootstrap_result.steps, 1):
        symbol = "OK" if step.success else "FAIL"
        line = f"  [{i}/{len(bootstrap_result.steps)}] {step.name} ... {symbol}"
        if step.already_done:
            line += " (already done)"
        if not step.success and step.error:
            line += f"\n    Error: {step.error}"
        lines.append(line)

    if bootstrap_result.success:
        lines.append("\nBootstrap complete. Server is ready for deploy.")
    else:
        lines.append("\nBootstrap failed. Fix the error above and retry.")

    return "\n".join(lines)


def main() -> None:
    """Entry point for the fraisier-mcp command."""
    mcp.run()

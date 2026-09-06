"""Daemon module for socket-activated deployments.

This module contains the core deployment logic extracted from CLI commands,
adapted to accept JSON deployment requests instead of command-line arguments.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fraisier.config import CONFIG_SEARCH_LOCATIONS, get_config
from fraisier.errors import DeploymentLockError
from fraisier.logging import get_contextual_logger

logger = get_contextual_logger("fraisier.daemon")


@dataclass
class DeploymentRequest:
    """Deployment request parsed from JSON input."""

    version: int
    project: str
    environment: str
    branch: str
    timestamp: str
    triggered_by: str
    options: dict[str, Any]
    metadata: dict[str, Any]


@dataclass
class DeploymentResult:
    """Result of daemon deployment execution."""

    success: bool
    status: str
    message: str | None = None
    deployed_version: str | None = None
    duration_seconds: float = 0.0
    error_message: str | None = None


def parse_deployment_request(json_str: str) -> DeploymentRequest:
    """Parse JSON string into DeploymentRequest.

    Args:
        json_str: JSON string containing deployment request

    Returns:
        Parsed DeploymentRequest

    Raises:
        ValueError: If JSON is invalid or missing required fields
    """
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}") from e

    # Validate version
    version = data.get("version")
    if version != 1:
        raise ValueError(f"Unsupported version: {version}")

    # Required fields
    required_fields = ["project", "environment", "branch", "timestamp", "triggered_by"]
    for field in required_fields:
        if field not in data:
            raise ValueError(f"Missing required field: {field}")

    # Validate environment
    valid_environments = ["development", "staging", "production"]
    if data["environment"] not in valid_environments:
        raise ValueError(f"Invalid environment: {data['environment']}")

    return DeploymentRequest(
        version=version,
        project=data["project"],
        environment=data["environment"],
        branch=data["branch"],
        timestamp=data["timestamp"],
        triggered_by=data["triggered_by"],
        options=data.get("options", {}),
        metadata=data.get("metadata", {}),
    )


def execute_deployment_request(request: DeploymentRequest) -> DeploymentResult:
    """Execute a deployment based on the request.

    Args:
        request: Parsed deployment request

    Returns:
        DeploymentResult with success/failure status
    """
    with logger.context(
        project=request.project,
        environment=request.environment,
        branch=request.branch,
        triggered_by=request.triggered_by,
        timestamp=request.timestamp,
    ):
        logger.info("Starting deployment", event="deployment_started")

    # Set to the deployer once its own record is open — i.e. once execute() has
    # been entered and has written `deploying`. Nothing before that point has
    # touched the status file.
    open_record: Any = None

    try:
        # Get configuration
        config = get_config()
        fraise_config = config.get_fraise_environment(
            request.project, request.environment
        )

        if not fraise_config:
            error_msg = _format_project_not_found_error(config, request.project)
            logger.error(
                "Project not found in configuration",
                event="deployment_failed",
                project=request.project,
                config_path=config.config_path,
            )
            return DeploymentResult(
                success=False,
                status="failed",
                message="Project not found",
                error_message=error_msg,
            )

        # Check that we're running as the correct deploy user
        import pwd

        current_user = pwd.getpwuid(os.getuid()).pw_name
        expected_user = config.get_deploy_user(request.project, request.environment)
        if current_user != expected_user:
            error_msg = (
                f"Deployment must run as '{expected_user}'"
                f" but running as '{current_user}'.\n"
                f"Run with: sudo -u {expected_user} fraisier deploy-daemon ..."
            )
            logger.error(
                "Wrong user for deployment",
                event="deployment_failed",
                current_user=current_user,
                expected_user=expected_user,
            )
            return DeploymentResult(
                success=False,
                status="failed",
                message="Wrong user",
                error_message=error_msg,
            )

        # Handle dry-run mode
        if request.options.get("dry_run"):
            logger.info(
                "Dry-run mode: showing deployment plan", event="deployment_dry_run"
            )

            # Get deployer to show what would happen
            fraise_type: str = fraise_config.get("type") or ""
            deployer = _get_deployer(fraise_type, fraise_config)

            if deployer is None:
                raise ValueError(f"Unknown fraise type '{fraise_config.get('type')}'")

            # Check if deployment is needed
            force = request.options.get("force", False)
            if not force and not deployer.is_deployment_needed():
                current_version = deployer.get_current_version()
                latest_version = deployer.get_latest_version()
                logger.info(
                    "Dry-run: No deployment needed, versions match",
                    event="deployment_dry_run_skipped",
                    current_version=current_version,
                    latest_version=latest_version,
                )
                return DeploymentResult(
                    success=True,
                    status="dry_run_no_changes",
                    message=f"Dry-run: Already up to date (current: {current_version})",
                    deployed_version=current_version,
                )

            # Show what would be deployed
            current_version = deployer.get_current_version()
            latest_version = deployer.get_latest_version()
            logger.info(
                "Dry-run: Would deploy",
                event="deployment_dry_run_plan",
                current_version=current_version,
                latest_version=latest_version,
                changes=f"{current_version} -> {latest_version}",
            )

            return DeploymentResult(
                success=True,
                status="dry_run_plan",
                message=f"Dry-run: Would deploy {current_version} -> {latest_version}",
                deployed_version=current_version,
            )

        # Set deployment options from request
        if request.options.get("force"):
            # Force deployment even if versions match
            pass  # This will be handled by deployer logic

        # Get deployer (reuse existing logic)
        fraise_type_str: str = fraise_config.get("type") or ""
        deployer = _get_deployer(fraise_type_str, fraise_config)

        if deployer is None:
            raise ValueError(f"Unknown fraise type '{fraise_config.get('type')}'")

        # Check if deployment is needed
        force = request.options.get("force", False)
        if not force and not deployer.is_deployment_needed():
            current_version = deployer.get_current_version()
            latest_version = deployer.get_latest_version()
            logger.info(
                "Deployment not needed, versions match",
                event="deployment_skipped",
                current_version=current_version,
                latest_version=latest_version,
            )
            return DeploymentResult(
                success=True,
                status="skipped",
                message="Already up to date",
                deployed_version=current_version,
            )

        # The status file belongs to the deployer, from `deploying` through to
        # the terminal state (#378). It stamps the owner fields and honours the
        # fraise's own status_dir; a write from here knows neither, and the one
        # that used to sit after execute() reported `success` for every result,
        # `rollback_failed` included.
        from fraisier.locking import deployment_lock

        try:
            with deployment_lock(request.project):
                open_record = deployer
                result = deployer.execute()
        except DeploymentLockError as e:
            # The record belongs to the deploy that holds the lock. Writing
            # `failed` here would bury it while it is still running.
            logger.warning(
                "Deployment refused: another deploy holds the lock",
                event="deployment_refused",
                error=str(e),
            )
            return DeploymentResult(
                success=False,
                status="failed",
                message="Deploy already running",
                error_message=str(e),
            )

        # Convert to daemon result format
        daemon_result = DeploymentResult(
            success=result.success,
            status=result.status.value,
            message="Deployment completed" if result.success else result.error_message,
            deployed_version=result.new_version,
            duration_seconds=result.duration_seconds,
            error_message=result.error_message if not result.success else None,
        )

        logger.info(
            "Deployment completed",
            event="deployment_completed",
            success=result.success,
            status=result.status.value,
            duration_seconds=result.duration_seconds,
            deployed_version=result.new_version,
        )

        return daemon_result

    except FileNotFoundError:
        error_msg = _format_config_not_found_error()
        logger.error(
            "Configuration file not found",
            event="deployment_failed",
            error=error_msg,
        )
        return DeploymentResult(
            success=False,
            status="failed",
            message="Configuration file not found",
            error_message=error_msg,
        )

    except Exception as e:
        # An exception that escaped execute() leaves the deployer's record at
        # `deploying` with nobody left to close it. Close it through the
        # deployer's own writer so the owner fields and status_dir are the ones
        # the rest of the record already carries (#378). Best effort: a failure
        # here must not replace the error being reported.
        if open_record is not None:
            try:
                open_record._write_status("failed", error_message=str(e))
            except Exception as write_exc:  # pragma: no cover - defensive
                logger.warning(
                    "Could not close the deployment record",
                    event="status_write_failed",
                    error=str(write_exc),
                )

        logger.error(
            "Deployment failed",
            event="deployment_failed",
            error=str(e),
        )
        return DeploymentResult(
            success=False,
            status="failed",
            message="Deployment failed",
            error_message=str(e),
        )


def _format_config_not_found_error() -> str:
    """Format diagnostic message when config file is not found."""
    lines = ["Configuration file 'fraises.yaml' not found."]

    # Check FRAISIER_CONFIG environment variable
    fraisier_config = os.environ.get("FRAISIER_CONFIG")
    if fraisier_config:
        lines.append(f"FRAISIER_CONFIG is set to: {fraisier_config}")
        if not Path(fraisier_config).exists():
            lines.append("  (file does not exist)")
    else:
        lines.append("FRAISIER_CONFIG environment variable is not set.")

    # List search locations
    lines.append("")
    lines.append("Searched locations:")
    for loc in CONFIG_SEARCH_LOCATIONS:
        exists = "✓" if loc.exists() else "✗"
        lines.append(f"  {exists} {loc}")

    # Systemd hint
    lines.append("")
    lines.append("Hint: In systemd units, set the environment variable using:")
    lines.append("  Environment=FRAISIER_CONFIG=/path/to/fraises.yaml")

    return "\n".join(lines)


def _format_project_not_found_error(config, project: str) -> str:
    """Format diagnostic message when project is not found in config."""
    lines = [f"Project '{project}' not found in configuration."]

    # Show loaded config file
    lines.append(f"Configuration loaded from: {config.config_path}")

    # List available projects
    available = config.list_fraises()
    if available:
        lines.append(f"Available projects: {', '.join(available)}")
    else:
        lines.append("No projects defined in configuration.")

    return "\n".join(lines)


def _get_deployer(fraise_type: str, fraise_config: dict):
    """Get appropriate deployer for fraise type.

    Extracted from cli/_helpers.py for reuse in daemon.
    """
    # Always use a local runner: the daemon process runs on the target host,
    # so the ssh: block (intended for client-side CLI commands) must not be
    # applied here.
    from fraisier.runners import LocalRunner

    runner = LocalRunner()

    if fraise_type == "api":
        from fraisier.deployers.api import APIDeployer

        return APIDeployer(fraise_config, runner=runner)

    elif fraise_type == "etl":
        from fraisier.deployers.etl import ETLDeployer

        return ETLDeployer(fraise_config, runner=runner)

    elif fraise_type in ("scheduled", "backup"):
        from fraisier.deployers.scheduled import ScheduledDeployer

        return ScheduledDeployer(fraise_config, runner=runner)

    elif fraise_type == "docker_compose":
        from fraisier.deployers.docker_compose import DockerComposeDeployer

        return DockerComposeDeployer(fraise_config, runner=runner)

    return None

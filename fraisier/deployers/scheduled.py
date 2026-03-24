"""Scheduled fraise deployer - for cron jobs and timers."""

import logging
import subprocess
import time
from typing import Any

from .base import BaseDeployer, DeploymentResult, DeploymentStatus
from .mixins import GitDeployMixin

logger = logging.getLogger("fraisier")


class ScheduledDeployer(GitDeployMixin, BaseDeployer):
    """Deployer for scheduled/cron job fraises.

    Pulls code via bare repo pattern, then manages systemd timers.
    """

    def __init__(self, config: dict[str, Any], runner: Any = None):
        super().__init__(config, runner=runner)
        self._init_git_deploy(config)
        self.systemd_service = config.get("systemd_service")
        self.systemd_timer = config.get("systemd_timer")
        self.script_path = config.get("script_path")
        self.job_name = config.get("job_name")

        from fraisier.dbops._validation import validate_service_name

        if self.systemd_timer:
            validate_service_name(self.systemd_timer)
        if self.systemd_service:
            validate_service_name(self.systemd_service)

    def is_deployment_needed(self) -> bool:
        """Check if timer needs to be enabled/restarted."""
        if not self.systemd_timer:
            return False

        try:
            result = self.runner.run(
                ["systemctl", "is-active", self.systemd_timer],
                check=False,
            )
            return result.returncode != 0
        except subprocess.CalledProcessError:
            return True

    def execute(self) -> DeploymentResult:
        """Execute scheduled job deployment.

        1. Pull code via bare repo (if app_path configured)
        2. Enable and start systemd timer
        """

        def _steps() -> tuple[str | None, str | None]:
            new_sha = None
            old_version = None

            if self.app_path:
                logger.info(f"Pulling code for scheduled job to {self.app_path}")
                old_sha, new_sha = self._git_pull()
                old_version = old_sha[:8] if old_sha else None

            if self.systemd_timer:
                logger.info(f"Enabling timer: {self.systemd_timer}")
                self.runner.run(
                    ["sudo", "systemctl", "enable", self.systemd_timer],
                )
                self.runner.run(
                    ["sudo", "systemctl", "start", self.systemd_timer],
                )
                self.runner.run(
                    ["sudo", "systemctl", "daemon-reload"],
                )

            new_version = new_sha[:8] if new_sha else self._get_timer_state()
            return old_version, new_version

        return self._execute_with_lifecycle(_steps)

    def _get_timer_state(self) -> str | None:
        """Get timer active state as version proxy."""
        if not self.systemd_timer:
            return None
        try:
            result = self.runner.run(
                [
                    "systemctl",
                    "show",
                    self.systemd_timer,
                    "--property=ActiveState",
                ],
            )
            parts = result.stdout.strip().split("=")
            state = parts[1] if len(parts) > 1 else "unknown"
            return f"timer:{state}"
        except (subprocess.CalledProcessError, IndexError):
            return None

    def health_check(self) -> bool:
        """Check if timer is active."""
        if not self.systemd_timer:
            return True
        try:
            result = self.runner.run(
                ["systemctl", "is-active", self.systemd_timer],
                check=False,
            )
            return result.returncode == 0
        except subprocess.CalledProcessError:
            return False

    def rollback(self, to_version: str | None = None) -> DeploymentResult:
        """Rollback: stop/disable timer, optionally revert git."""
        start_time = time.time()
        current_version = self.get_current_version() or self._get_timer_state()

        try:
            if self.systemd_timer:
                logger.info(f"Stopping timer: {self.systemd_timer}")
                self.runner.run(
                    ["sudo", "systemctl", "stop", self.systemd_timer],
                )
                self.runner.run(
                    ["sudo", "systemctl", "disable", self.systemd_timer],
                )

            new_version = self._get_timer_state()
            duration = time.time() - start_time

            self._write_status("rolled_back")
            return DeploymentResult(
                success=True,
                status=DeploymentStatus.ROLLED_BACK,
                old_version=current_version,
                new_version=new_version,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = time.time() - start_time
            logger.exception(f"Scheduled job rollback failed: {e}")

            self._write_status("failed", error_message=f"Rollback failed: {e}")
            return DeploymentResult(
                success=False,
                status=DeploymentStatus.FAILED,
                old_version=current_version,
                duration_seconds=duration,
                error_message=f"Rollback failed: {e}",
            )

"""ETL fraise deployer - for data pipeline jobs."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fraisier.runners import CommandRunner

from .base import BaseDeployer, DeploymentResult, DeploymentStatus
from .mixins import GitDeployMixin

logger = logging.getLogger("fraisier")


class ETLDeployer(GitDeployMixin, BaseDeployer):
    """Deployer for ETL/data pipeline fraises.

    Uses bare repo pattern for git operations, then runs configured scripts.
    """

    def __init__(self, config: dict[str, Any], runner: CommandRunner | None = None):
        super().__init__(config, runner=runner)
        self._init_git_deploy(config)
        self.script_path = config.get("script_path")
        self.database_config = config.get("database", {})

    def execute(self) -> DeploymentResult:
        """Execute ETL deployment.

        1. Pull code via bare repo
        2. Run configured script (if any)
        """

        def _steps() -> tuple[str | None, str | None]:
            new_sha = None
            old_version = None

            if self.app_path:
                logger.info(f"Deploying ETL via bare repo to {self.app_path}")
                old_sha, new_sha = self._git_pull()
                old_version = old_sha[:8] if old_sha else None

            if self.script_path and self.app_path:
                logger.info(f"Running ETL script: {self.script_path}")
                self.runner.run(
                    ["python", self.script_path],
                    cwd=self.app_path,
                )

            new_version = new_sha[:8] if new_sha else None
            return old_version, new_version

        return self._execute_with_lifecycle(_steps)

    def rollback(self, to_version: str | None = None) -> DeploymentResult:
        """Rollback ETL deployment via bare repo checkout."""
        start_time = time.time()
        current_version = self.get_current_version()
        target = to_version or self._previous_sha

        try:
            if not target:
                raise ValueError("No previous SHA available for rollback")

            self._git_rollback(target)

            new_version = target[:8]
            duration = time.time() - start_time

            self._write_status("rolled_back", commit_sha=target)
            return DeploymentResult(
                success=True,
                status=DeploymentStatus.ROLLED_BACK,
                old_version=current_version,
                new_version=new_version,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = time.time() - start_time
            self._write_status("failed", error_message=f"Rollback failed: {e}")
            return DeploymentResult(
                success=False,
                status=DeploymentStatus.FAILED,
                old_version=current_version,
                duration_seconds=duration,
                error_message=f"Rollback failed: {e}",
            )

"""ETL fraise deployer - for data pipeline jobs."""

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from .base import BaseDeployer, DeploymentResult, DeploymentStatus
from .mixins import GitDeployMixin

logger = logging.getLogger("fraisier")


class ETLDeployer(GitDeployMixin, BaseDeployer):
    """Deployer for ETL/data pipeline fraises.

    Uses bare repo pattern for git operations, then runs configured scripts.
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self._init_git_deploy(config)
        self.script_path = config.get("script_path")
        self.database_config = config.get("database", {})

    def execute(self) -> DeploymentResult:
        """Execute ETL deployment.

        1. Pull code via bare repo
        2. Run configured script (if any)
        """
        start_time = time.time()
        old_version = None
        self._write_status("deploying")
        db_pk = self._start_db_record()

        try:
            # Step 1: Git pull via bare repo
            if self.app_path:
                logger.info(f"Deploying ETL via bare repo to {self.app_path}")
                old_sha, new_sha = self._git_pull()
                old_version = old_sha[:8] if old_sha else None
            else:
                new_sha = None

            # Step 2: Run configured script
            if self.script_path and self.app_path:
                logger.info(f"Running ETL script: {self.script_path}")
                subprocess.run(
                    ["python", self.script_path],
                    cwd=self.app_path,
                    check=True,
                    capture_output=True,
                    text=True,
                )

            new_version = new_sha[:8] if new_sha else None
            duration = time.time() - start_time

            self._write_status("success", commit_sha=new_sha)
            result = DeploymentResult(
                success=True,
                status=DeploymentStatus.SUCCESS,
                old_version=old_version,
                new_version=new_version,
                duration_seconds=duration,
            )
            self._complete_db_record(db_pk, result)
            return result

        except Exception as e:
            duration = time.time() - start_time
            logger.exception(f"ETL deployment failed: {e}")
            wrapped = self._wrap_error(e)

            self._write_status("failed", error_message=str(e))
            result = DeploymentResult(
                success=False,
                status=DeploymentStatus.FAILED,
                old_version=old_version,
                duration_seconds=duration,
                error_message=str(e),
                error=wrapped,
            )
            self._complete_db_record(db_pk, result)
            return result

    def rollback(self, to_version: str | None = None) -> DeploymentResult:
        """Rollback ETL deployment via bare repo checkout."""
        start_time = time.time()
        current_version = self.get_current_version()
        target = to_version or self._previous_sha

        try:
            if not target:
                raise ValueError("No previous SHA available for rollback")

            worktree = Path(self.app_path)
            subprocess.run(
                [
                    "git",
                    f"--work-tree={worktree}",
                    f"--git-dir={self.bare_repo}",
                    "checkout",
                    "-f",
                    target,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree), "reset", "--soft", target],
                check=True,
                capture_output=True,
                text=True,
            )

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

"""Shared deployer mixins for common deployment patterns."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fraisier.errors import DeploymentError, FraisierError
from fraisier.git.operations import (
    clone_bare_repo,
    fetch_and_checkout,
    get_worktree_sha,
)
from fraisier.status import DEFAULT_STATUS_DIR, DeploymentStatusFile, write_status

if TYPE_CHECKING:
    from fraisier.deployers.base import DeploymentResult

logger = logging.getLogger("fraisier")

DEFAULT_REPOS_BASE = Path("/var/lib/fraisier/repos")


class GitDeployMixin:
    """Mixin providing bare-repo git operations and status file writing.

    Expects the consuming class to set self.fraise_name and self.environment
    (typically via BaseDeployer.__init__).
    """

    def _init_git_deploy(self, config: dict[str, Any]) -> None:
        """Initialize git deploy fields from config."""
        self.clone_url = config.get("clone_url")
        self.app_path = config.get("app_path")
        self.branch = config.get("branch", "main")
        repos_base = config.get("repos_base", str(DEFAULT_REPOS_BASE))
        self.bare_repo = Path(repos_base) / f"{self.fraise_name}.git"
        self.status_dir = Path(config.get("status_dir", str(DEFAULT_STATUS_DIR)))
        self._previous_sha: str | None = None

    def get_current_version(self) -> str | None:
        """Get currently deployed git commit from worktree."""
        if not self.app_path:
            return None
        sha = get_worktree_sha(Path(self.app_path))
        return sha[:8] if sha else None

    def get_latest_version(self) -> str | None:
        """Get latest git commit from remote via bare repo."""
        import subprocess

        if not self.bare_repo.exists():
            return None
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.bare_repo),
                    "rev-parse",
                    f"origin/{self.branch}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()[:8]
        except subprocess.CalledProcessError:
            return None

    def _git_pull(self) -> tuple[str | None, str]:
        """Clone bare repo if needed, then fetch + checkout.

        Returns (old_sha, new_sha).
        """
        if self.clone_url:
            clone_bare_repo(self.clone_url, self.bare_repo)
        old_sha, new_sha = fetch_and_checkout(
            self.bare_repo, Path(self.app_path), self.branch
        )
        self._previous_sha = old_sha
        return old_sha, new_sha

    def _wrap_error(self, exc: Exception) -> FraisierError:
        """Wrap a bare exception into a structured FraisierError."""
        ctx = {"fraise": self.fraise_name, "environment": self.environment}
        if isinstance(exc, FraisierError):
            exc.context.update(ctx)
            return exc
        return DeploymentError(str(exc), context=ctx, cause=exc)

    def _write_status(self, state: str, **kwargs: Any) -> None:
        """Write deployment status file."""
        status = DeploymentStatusFile(
            fraise_name=self.fraise_name,
            environment=self.environment,
            state=state,
            **kwargs,
        )
        try:
            write_status(status, status_dir=self.status_dir)
        except OSError:
            logger.warning("Failed to write status file")

    def _start_db_record(
        self,
        old_version: str | None = None,
        git_commit: str | None = None,
    ) -> int | None:
        """Record deployment start in database. Returns deployment pk."""
        try:
            from fraisier.database import get_db

            db = get_db()
            return db.start_deployment(
                fraise=self.fraise_name,
                environment=self.environment,
                triggered_by="deploy",
                git_branch=getattr(self, "branch", None),
                git_commit=git_commit,
                old_version=old_version,
            )
        except Exception:
            logger.warning("Failed to record deployment start in DB")
            return None

    def _complete_db_record(
        self,
        deployment_pk: int | None,
        result: DeploymentResult,
    ) -> None:
        """Record deployment completion in database."""
        if deployment_pk is None:
            return
        try:
            from fraisier.database import get_db

            db = get_db()
            db.complete_deployment(
                deployment_id=deployment_pk,
                success=result.success,
                new_version=result.new_version,
                error_message=result.error_message,
            )
            if result.status.value == "rolled_back":
                db.mark_deployment_rolled_back(deployment_pk)
        except Exception:
            logger.warning("Failed to record deployment completion in DB")

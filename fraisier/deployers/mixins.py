"""Shared deployer mixins for common deployment patterns."""

from __future__ import annotations

import logging
import shlex
import sqlite3
import subprocess
import time
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
    from collections.abc import Callable

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
        git_repo = config.get("git_repo")
        if git_repo:
            self.bare_repo = Path(git_repo)
        else:
            repos_base = config.get("repos_base", str(DEFAULT_REPOS_BASE))
            self.bare_repo = Path(repos_base) / f"{self.fraise_name}.git"
        self.status_dir = Path(config.get("status_dir", str(DEFAULT_STATUS_DIR)))
        self.lock_timeout = config.get("lock_timeout", 300)
        self._previous_sha: str | None = None
        install_config = config.get("install", {})
        self.install_command: list[str] | None = install_config.get("command")
        self.install_user: str | None = install_config.get("user")
        self._init_notifications(config)

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

    def _install_dependencies(self) -> None:
        """Run dependency install command if configured.

        Runs the configured install command in the app_path directory.
        When install.user is set and differs from deploy_user, the command
        is prefixed with sudo -u. When they are the same, runs directly.

        Raises:
            DeploymentError: If the install command fails, with detailed context
                including the command, exit code, stdout, stderr, and a suggested
                debugging command.
        """
        if not self.install_command or not self.app_path:
            return
        cmd = list(self.install_command)
        # Only use sudo if install_user differs from deploy_user
        deploy_user = self.config.get("deploy_user")
        if self.install_user and self.install_user != deploy_user:
            cmd = ["sudo", "-u", self.install_user, *cmd]
        logger.info("Installing dependencies: %s", cmd)
        try:
            self.runner.run(cmd, cwd=self.app_path)
        except subprocess.CalledProcessError as exc:
            suggested = f"cd {self.app_path} && {shlex.join(cmd)}"
            raise DeploymentError(
                f"Install command failed (exit code {exc.returncode}): "
                f"{shlex.join(exc.cmd) if isinstance(exc.cmd, list) else exc.cmd}\n"
                f"  Directory: {self.app_path}\n"
                f"  To debug: {suggested}",
                context={
                    "command": exc.cmd,
                    "cwd": self.app_path,
                    "exit_code": exc.returncode,
                    "stdout": exc.stdout or "",
                    "stderr": exc.stderr or "",
                    "suggested_command": suggested,
                },
            ) from exc

    def _git_rollback(
        self,
        target: str,
        runner: Any | None = None,
    ) -> None:
        """Rollback worktree to *target* SHA via bare repo checkout."""
        r = runner or self.runner
        worktree = Path(self.app_path)
        r.run(
            [
                "git",
                f"--work-tree={worktree}",
                f"--git-dir={self.bare_repo}",
                "checkout",
                "-f",
                target,
            ],
        )
        r.run(
            [
                "git",
                f"--work-tree={worktree}",
                f"--git-dir={self.bare_repo}",
                "reset",
                "--soft",
                target,
            ],
        )

    def _wrap_error(self, exc: Exception) -> FraisierError:
        """Wrap a bare exception into a structured FraisierError."""
        ctx = {"fraise": self.fraise_name, "environment": self.environment}
        if isinstance(exc, FraisierError):
            exc.context.update(ctx)
            return exc
        return DeploymentError(str(exc), context=ctx, cause=exc)

    def _init_notifications(self, config: dict[str, Any]) -> None:
        """Initialize notification dispatcher from config."""
        from fraisier.notifications.dispatcher import NotificationDispatcher

        notifications_config = config.get("notifications", {})
        if notifications_config:
            self._dispatcher = NotificationDispatcher.from_config(notifications_config)
        else:
            self._dispatcher = NotificationDispatcher()

    def _notify(self, result: DeploymentResult) -> None:
        """Send deployment notifications (fire-and-forget)."""
        from fraisier.notifications.base import DeployEvent

        if not self._dispatcher.is_configured:
            return
        event = DeployEvent.from_result(
            result=result,
            fraise_name=self.fraise_name,
            environment=self.environment,
        )
        self._dispatcher.notify(event)

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
        except OSError as exc:
            logger.warning(
                "Failed to write status file for %s (dir=%s): %s",
                self.fraise_name,
                self.status_dir,
                exc,
            )

    def _start_db_record(
        self,
        old_version: str | None = None,
        git_commit: str | None = None,
    ) -> int | None:
        """Record deployment start in database. Returns deployment pk."""
        try:
            import getpass

            from fraisier.database import get_db, get_db_path

            db = get_db()
            return db.start_deployment(
                fraise=self.fraise_name,
                environment=self.environment,
                triggered_by="deploy",
                triggered_by_user=getpass.getuser(),
                git_branch=getattr(self, "branch", None),
                git_commit=git_commit,
                old_version=old_version,
            )
        except (sqlite3.Error, OSError) as exc:
            db_path = get_db_path()
            logger.warning(
                "Failed to record deployment start in DB for %s/%s (db=%s): %s",
                self.fraise_name,
                self.environment,
                db_path,
                exc,
            )
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
        except (sqlite3.Error, OSError) as exc:
            from fraisier.database import get_db_path

            db_path = get_db_path()
            logger.warning(
                "Failed to record deployment completion in DB (pk=%s, db=%s): %s",
                deployment_pk,
                db_path,
                exc,
            )

    def _write_incident(
        self,
        error_message: str,
        *,
        current_version: str | None = None,
        target_version: str | None = None,
        db_errors: list[str] | None = None,
    ) -> None:
        """Write an incident file for manual recovery.

        Creates a JSON file in ``/var/lib/fraisier/incidents/`` with full
        context for operators to diagnose and recover from failed rollbacks.
        """
        import json
        from datetime import UTC, datetime

        incidents_dir = Path("/var/lib/fraisier/incidents")
        try:
            incidents_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            filename = f"{self.fraise_name}_{timestamp}.json"
            incident = {
                "fraise": self.fraise_name,
                "environment": self.environment,
                "timestamp": timestamp,
                "error": error_message,
                "current_version": current_version,
                "target_version": target_version,
                "db_errors": db_errors or [],
                "branch": getattr(self, "branch", None),
            }
            (incidents_dir / filename).write_text(json.dumps(incident, indent=2))
            logger.info("Incident file written: %s/%s", incidents_dir, filename)
        except OSError:
            logger.warning("Failed to write incident file")

    def _execute_with_lifecycle(
        self,
        steps_fn: Callable[[], tuple[str | None, str | None]],
    ) -> DeploymentResult:
        """Run deployment steps with timing, status tracking, and DB recording.

        Args:
            steps_fn: Callable that executes the service-specific deployment
                steps.  Must return ``(old_version, new_version)``.

        Returns:
            DeploymentResult with success/failure status and timing.
        """
        from fraisier.deployers.base import DeploymentResult, DeploymentStatus

        start_time = time.time()
        self._write_status("deploying")
        db_pk = self._start_db_record()

        try:
            old_version, new_version = steps_fn()
            duration = time.time() - start_time

            self._write_status("success", commit_sha=new_version)
            result = DeploymentResult(
                success=True,
                status=DeploymentStatus.SUCCESS,
                old_version=old_version,
                new_version=new_version,
                duration_seconds=duration,
            )
            self._complete_db_record(db_pk, result)
            self._notify(result)
            return result

        except Exception as e:
            duration = time.time() - start_time
            logger.exception(f"Deployment failed: {e}")
            wrapped = self._wrap_error(e)

            self._write_status("failed", error_message=str(e))
            result = DeploymentResult(
                success=False,
                status=DeploymentStatus.FAILED,
                old_version=None,
                duration_seconds=duration,
                error_message=str(e),
                error=wrapped,
            )
            self._complete_db_record(db_pk, result)
            self._notify(result)
            return result

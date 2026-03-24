"""API fraise deployer - for web services and APIs."""

import logging
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from fraisier.errors import DeploymentError, HealthCheckError
from fraisier.health_check import HealthCheckManager, HTTPHealthChecker

from .base import BaseDeployer, DeploymentResult, DeploymentStatus
from .mixins import GitDeployMixin

logger = logging.getLogger("fraisier")


class _DeploymentTimeout(Exception):
    """Raised when deployment exceeds configured timeout."""


def _arm_timeout(timeout: int) -> Any:
    """Set a SIGALRM timeout, returning the previous handler."""

    def _handler(_signum: int, _frame: Any) -> None:
        raise _DeploymentTimeout(f"Deployment timed out after {timeout} seconds")

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(timeout)
    return old_handler


def _disarm_timeout(old_handler: Any) -> None:
    """Cancel any pending SIGALRM and restore the previous handler."""
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old_handler)


class APIDeployer(GitDeployMixin, BaseDeployer):
    """Deployer for API/web service fraises.

    Handles:
    - Bare repo + worktree git operations (via GitDeployMixin)
    - Database migrations
    - Service restart via systemd
    - Health check verification
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self._init_git_deploy(config)
        self.git_repo = config.get("git_repo")
        self.systemd_service = config.get("systemd_service")
        self.health_check_url = config.get("health_check", {}).get("url")
        self.health_check_timeout = config.get("health_check", {}).get("timeout", 30)
        self.database_config = config.get("database", {})
        self.allow_irreversible = config.get("allow_irreversible", False)
        self._migrations_applied: int = 0

    def execute(self) -> DeploymentResult:
        """Execute API deployment."""
        start_time = time.time()
        old_version = None
        self._write_status("deploying")
        db_pk = self._start_db_record()

        timeout = self.config.get("timeout", 600)
        old_handler = _arm_timeout(timeout)

        try:
            # Step 1: Git pull via bare repo
            logger.info(f"Deploying via bare repo to {self.app_path}")
            old_sha, new_sha = self._git_pull()
            old_version = old_sha[:8] if old_sha else None

            # Step 2: Run database migrations via strategy if configured
            if self.database_config:
                logger.info("Running database migrations")
                self._run_strategy()

            # Step 3: Restart service
            if self.systemd_service:
                logger.info(f"Restarting service: {self.systemd_service}")
                self._restart_service()

            # Step 4: Health check
            if self.health_check_url:
                logger.info(f"Running health check: {self.health_check_url}")
                if not self._wait_for_health():
                    if self._previous_sha:
                        logger.warning(
                            "Health check failed, rolling back to "
                            f"{self._previous_sha[:8]}"
                        )
                        rollback_result = self.rollback()
                        duration = time.time() - start_time
                        result = self._build_rollback_result(
                            rollback_result,
                            old_version,
                            duration,
                        )
                        self._complete_db_record(db_pk, result)
                        return result
                    raise HealthCheckError(
                        "Health check failed after deployment",
                        context={
                            "fraise": self.fraise_name,
                            "environment": self.environment,
                            "url": self.health_check_url,
                        },
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

        except _DeploymentTimeout as e:
            duration = time.time() - start_time
            logger.error(str(e))
            self._write_status("failed", error_message=str(e))
            result = DeploymentResult(
                success=False,
                status=DeploymentStatus.FAILED,
                old_version=old_version,
                duration_seconds=duration,
                error_message=str(e),
            )
            self._complete_db_record(db_pk, result)
            return result

        except Exception as e:
            duration = time.time() - start_time
            logger.exception(f"Deployment failed: {e}")
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

        finally:
            _disarm_timeout(old_handler)

    def _run_strategy(self) -> None:
        """Run database migrations via deployment strategy."""
        from fraisier.strategies import get_strategy

        strategy_name = self.database_config.get("strategy", "apply")

        # Map config names to strategy names
        strategy_map = {"apply": "migrate", "rebuild": "rebuild", "migrate": "migrate"}
        resolved = strategy_map.get(strategy_name, strategy_name)

        strategy = get_strategy(resolved)
        confiture_config = Path(
            self.database_config.get("confiture_config", "confiture.yaml")
        )
        migrations_dir = Path(
            self.database_config.get("migrations_dir", "db/migrations")
        )

        result = strategy.execute(
            confiture_config,
            migrations_dir=migrations_dir,
            allow_irreversible=self.allow_irreversible,
        )

        self._migrations_applied = result.migrations_applied

        if not result.success:
            raise DeploymentError(
                "; ".join(result.errors) or "Database migration failed",
                context={
                    "strategy": resolved,
                    "fraise": self.fraise_name,
                },
            )

    def _restart_service(self) -> None:
        """Restart systemd service."""
        if not self.systemd_service:
            return
        from fraisier.dbops._validation import validate_service_name

        validate_service_name(self.systemd_service)
        subprocess.run(
            ["sudo", "systemctl", "restart", self.systemd_service],
            check=True,
            capture_output=True,
        )

    def _build_rollback_result(
        self,
        rollback_result: DeploymentResult,
        old_version: str | None,
        duration: float,
    ) -> DeploymentResult:
        """Build a DeploymentResult after a rollback attempt."""
        if rollback_result.success:
            return DeploymentResult(
                success=False,
                status=DeploymentStatus.ROLLED_BACK,
                old_version=old_version,
                new_version=rollback_result.new_version,
                duration_seconds=duration,
                error_message="Health check failed; rolled back successfully",
                error=HealthCheckError(
                    "Health check failed after deployment",
                    context={
                        "fraise": self.fraise_name,
                        "environment": self.environment,
                        "url": self.health_check_url,
                    },
                ),
            )

        logger.critical(
            "ROLLBACK FAILED — service may be in broken state. Rollback error: %s",
            rollback_result.error_message,
        )
        return DeploymentResult(
            success=False,
            status=DeploymentStatus.FAILED,
            old_version=old_version,
            duration_seconds=duration,
            error_message=(
                "Health check failed AND rollback failed: "
                f"{rollback_result.error_message}"
            ),
            error=DeploymentError(
                "Deployment and rollback both failed",
                context={
                    "fraise": self.fraise_name,
                    "environment": self.environment,
                    "health_check_url": self.health_check_url,
                    "rollback_error": rollback_result.error_message,
                },
            ),
        )

    def _wait_for_health(self) -> bool:
        """Wait for health check to pass with exponential backoff."""
        if not self.health_check_url:
            return True
        checker = HTTPHealthChecker(self.health_check_url)
        manager = HealthCheckManager(
            provider="bare_metal",
            deployment_id=f"{self.fraise_name}-{self.environment}",
        )
        result = manager.check_with_retries(
            checker,
            max_retries=10,
            initial_delay=1.0,
            backoff_factor=2.0,
            max_delay=30.0,
            timeout=self.health_check_timeout,
        )
        return result.success

    def health_check(self) -> bool:
        """Check if API is healthy (single attempt, no retries)."""
        if not self.health_check_url:
            return True
        checker = HTTPHealthChecker(self.health_check_url)
        manager = HealthCheckManager()
        result = manager.check_with_retries(
            checker, max_retries=1, timeout=self.health_check_timeout
        )
        return result.success

    def rollback(self, to_version: str | None = None) -> DeploymentResult:
        """Rollback to previous version: migrate down, then git checkout."""
        start_time = time.time()
        current_version = self.get_current_version()
        target = to_version or self._previous_sha

        try:
            if not target:
                raise ValueError("No previous SHA available for rollback")

            # Step 0: Roll back database migrations if any were applied
            if self._migrations_applied > 0 and self.database_config:
                from fraisier.strategies import get_strategy

                strategy_name = self.database_config.get("strategy", "apply")
                strategy_map = {
                    "apply": "migrate",
                    "rebuild": "rebuild",
                    "migrate": "migrate",
                }
                resolved = strategy_map.get(strategy_name, strategy_name)
                strategy = get_strategy(resolved)
                confiture_config = Path(
                    self.database_config.get("confiture_config", "confiture.yaml")
                )
                migrations_dir = Path(
                    self.database_config.get("migrations_dir", "db/migrations")
                )
                db_result = strategy.rollback(
                    confiture_config,
                    migrations_dir=migrations_dir,
                    steps=self._migrations_applied,
                )
                if not db_result.success:
                    logger.critical("Database rollback failed: %s", db_result.errors)

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

            if self.systemd_service:
                self._restart_service()
            if self.health_check_url:
                self._wait_for_health()

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

        except subprocess.CalledProcessError as e:
            duration = time.time() - start_time
            detail = (
                f"Rollback failed at command: {e.cmd!r}, "
                f"exit code: {e.returncode}, "
                f"stderr: {e.stderr}"
            )
            logger.critical(detail)
            self._write_status("failed", error_message=detail)
            return DeploymentResult(
                success=False,
                status=DeploymentStatus.FAILED,
                old_version=current_version,
                duration_seconds=duration,
                error_message=detail,
            )
        except Exception as e:
            duration = time.time() - start_time
            detail = f"Rollback failed: {type(e).__name__}: {e}"
            logger.critical(detail)
            self._write_status("failed", error_message=detail)
            return DeploymentResult(
                success=False,
                status=DeploymentStatus.FAILED,
                old_version=current_version,
                duration_seconds=duration,
                error_message=detail,
            )

"""API fraise deployer - for web services and APIs."""

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from fraisier.errors import DeploymentError, HealthCheckError
from fraisier.health_check import HealthCheckManager, HTTPHealthChecker
from fraisier.timeout import DeploymentTimeoutExpired, deployment_timeout

from .base import BaseDeployer, DeploymentResult, DeploymentStatus
from .mixins import GitDeployMixin

logger = logging.getLogger("fraisier")


class APIDeployer(GitDeployMixin, BaseDeployer):
    """Deployer for API/web service fraises.

    Handles:
    - Bare repo + worktree git operations (via GitDeployMixin)
    - Database migrations
    - Service restart via systemd
    - Health check verification
    """

    def __init__(self, config: dict[str, Any], runner: Any = None):
        super().__init__(config, runner=runner)
        self._init_git_deploy(config)
        self.git_repo = config.get("git_repo")
        self.systemd_service = config.get("systemd_service")
        hc = config.get("health_check", {})
        self.health_check_url = hc.get("url")
        self.health_check_timeout = hc.get("timeout", 30)
        self.health_check_retries = hc.get("retries", 5)
        self.database_config = config.get("database", {})
        self.allow_irreversible = config.get("allow_irreversible", False)
        self.lock_timeout = config.get("lock_timeout", 300)
        self._migrations_applied: int = 0

    def execute(self) -> DeploymentResult:
        """Execute API deployment."""
        start_time = time.time()
        old_version = None
        self._write_status("deploying")
        db_pk = self._start_db_record()

        timeout = self.config.get("timeout", 600)

        try:
            with deployment_timeout(timeout):
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

        except DeploymentTimeoutExpired as e:
            result = self._handle_timeout(e, old_version, start_time)
            self._complete_db_record(db_pk, result)
            return result

        except Exception as e:
            duration = time.time() - start_time
            logger.exception(f"Deployment failed: {e}")
            wrapped = self._wrap_error(e)
            self._restore_previous_state()

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

    def _handle_timeout(
        self,
        exc: DeploymentTimeoutExpired,
        old_version: str | None,
        start_time: float,
    ) -> DeploymentResult:
        """Handle a deployment timeout, attempting rollback if possible."""
        logger.error(str(exc))

        if self._previous_sha:
            logger.warning(
                "Timeout — attempting rollback to %s", self._previous_sha[:8]
            )
            rollback_result = self.rollback()
            duration = time.time() - start_time
            return self._build_timeout_rollback_result(
                rollback_result, old_version, duration, str(exc)
            )

        duration = time.time() - start_time
        self._write_status("failed", error_message=str(exc))
        return DeploymentResult(
            success=False,
            status=DeploymentStatus.FAILED,
            old_version=old_version,
            duration_seconds=duration,
            error_message=str(exc),
        )

    def _restore_previous_state(self) -> None:
        """Restore git and service to previous state after a failure."""
        if not self._previous_sha:
            return
        try:
            self._git_rollback(self._previous_sha)
            if self.systemd_service:
                self._restart_service()
        except Exception as rollback_exc:
            logger.critical("Git rollback after failure also failed: %s", rollback_exc)

    def _resolve_strategy(self) -> tuple[Any, Path, Path]:
        """Resolve database strategy, config path, and migrations dir from config."""
        from fraisier.strategies import get_strategy

        strategy_name = self.database_config.get("strategy", "apply")
        strategy_map = {"apply": "migrate", "rebuild": "rebuild", "migrate": "migrate"}
        resolved = strategy_map.get(strategy_name, strategy_name)

        strategy = get_strategy(resolved)
        confiture_config = Path(
            self.database_config.get("confiture_config", "confiture.yaml")
        )
        migrations_dir = Path(
            self.database_config.get("migrations_dir", "db/migrations")
        )
        return strategy, confiture_config, migrations_dir

    def _run_strategy(self) -> None:
        """Run database migrations via deployment strategy."""
        strategy, confiture_config, migrations_dir = self._resolve_strategy()

        pre_verify = self.database_config.get("pre_migrate_verify", False)
        result = strategy.execute(
            confiture_config,
            migrations_dir=migrations_dir,
            allow_irreversible=self.allow_irreversible,
            pre_migrate_verify=pre_verify,
        )

        self._migrations_applied = result.migrations_applied

        if not result.success:
            raise DeploymentError(
                "; ".join(result.errors) or "Database migration failed",
                context={
                    "strategy": strategy.__class__.__name__,
                    "fraise": self.fraise_name,
                },
            )

    def _restart_service(self) -> None:
        """Restart systemd service."""
        if not self.systemd_service:
            return
        from fraisier.systemd import SystemdServiceManager

        SystemdServiceManager(self.runner).restart(self.systemd_service)

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
        result = DeploymentResult(
            success=False,
            status=DeploymentStatus.ROLLBACK_FAILED,
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
        self._notify(result)
        return result

    def _build_timeout_rollback_result(
        self,
        rollback_result: DeploymentResult,
        old_version: str | None,
        duration: float,
        timeout_message: str,
    ) -> DeploymentResult:
        """Build a DeploymentResult after a rollback triggered by timeout."""
        if rollback_result.success:
            self._write_status("rolled_back", commit_sha=rollback_result.new_version)
            return DeploymentResult(
                success=False,
                status=DeploymentStatus.ROLLED_BACK,
                old_version=old_version,
                new_version=rollback_result.new_version,
                duration_seconds=duration,
                error_message=f"Timed out; rolled back successfully. {timeout_message}",
                error=DeploymentError(
                    timeout_message,
                    context={
                        "fraise": self.fraise_name,
                        "environment": self.environment,
                    },
                ),
            )

        logger.critical(
            "TIMEOUT ROLLBACK FAILED — service may be in broken state: %s",
            rollback_result.error_message,
        )
        self._write_status("failed", error_message=timeout_message)
        result = DeploymentResult(
            success=False,
            status=DeploymentStatus.ROLLBACK_FAILED,
            old_version=old_version,
            duration_seconds=duration,
            error_message=(
                f"Timed out AND rollback failed: {rollback_result.error_message}"
            ),
            error=DeploymentError(
                "Timeout and rollback both failed",
                context={
                    "fraise": self.fraise_name,
                    "environment": self.environment,
                    "rollback_error": rollback_result.error_message,
                },
            ),
        )
        self._notify(result)
        return result

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
            max_retries=self.health_check_retries,
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

    def _rollback_database(
        self, current_version: str | None, target: str
    ) -> DeploymentResult | None:
        """Roll back database migrations. Returns failure result or None."""
        strategy, confiture_config, migrations_dir = self._resolve_strategy()
        db_result = strategy.rollback(
            confiture_config,
            migrations_dir=migrations_dir,
            steps=self._migrations_applied,
        )
        if db_result.success:
            return DeploymentResult(
                success=True,
                status=DeploymentStatus.ROLLED_BACK,
                old_version=current_version,
                duration_seconds=0,
                details={
                    "migrations_rolled_back": db_result.migrations_applied,
                },
            )

        rolled_back = db_result.migrations_applied
        remaining = self._migrations_applied - rolled_back
        logger.critical("Database rollback failed: %s", db_result.errors)
        error_msg = (
            f"Database rollback failed — manual intervention required. "
            f"Errors: {'; '.join(db_result.errors)}. "
            f"Rolled back {rolled_back} of {self._migrations_applied} "
            f"migrations; {remaining} still applied. "
            f"Do NOT restart the service until resolved."
        )
        self._write_incident(
            error_msg,
            current_version=current_version,
            target_version=target,
            db_errors=db_result.errors,
        )
        self._write_status("failed", error_message=error_msg)
        return DeploymentResult(
            success=False,
            status=DeploymentStatus.FAILED,
            old_version=current_version,
            duration_seconds=0,
            error_message=error_msg,
        )

    def _finalize_rollback(
        self, current_version: str | None, target: str, start_time: float
    ) -> DeploymentResult:
        """Restart service, health-check, and return success result."""
        if self.systemd_service:
            self._restart_service()
        if self.health_check_url:
            self._wait_for_health()

        duration = time.time() - start_time
        self._write_status("rolled_back", commit_sha=target)
        return DeploymentResult(
            success=True,
            status=DeploymentStatus.ROLLED_BACK,
            old_version=current_version,
            new_version=target[:8],
            duration_seconds=duration,
        )

    def rollback(self, to_version: str | None = None) -> DeploymentResult:
        """Rollback to previous version: migrate down, then git checkout."""
        start_time = time.time()
        current_version = self.get_current_version()
        target = to_version or self._previous_sha

        try:
            if not target:
                raise ValueError("No previous SHA available for rollback")

            db_details: dict[str, int] = {}
            if self._migrations_applied > 0 and self.database_config:
                db_result = self._rollback_database(current_version, target)
                if not db_result.success:
                    db_result.duration_seconds = time.time() - start_time
                    return db_result
                db_details = db_result.details

            self._git_rollback(target)
            result = self._finalize_rollback(current_version, target, start_time)
            result.details.update(db_details)
            return result

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

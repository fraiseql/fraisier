"""Scheduled fraise deployer - for cron jobs and timers."""

from __future__ import annotations

import logging
import subprocess
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from fraisier.runners import CommandRunner
    from fraisier.service_managers.base import ServiceManager

from fraisier import naming
from fraisier.errors import DeploymentError

from .base import BaseDeployer, DeploymentResult, DeploymentStatus
from .mixins import GitDeployMixin

logger = logging.getLogger("fraisier")


class ScheduledDeployer(GitDeployMixin, BaseDeployer):
    """Deployer for scheduled/cron job fraises.

    Pulls code via bare repo pattern, then manages systemd timers.
    """

    def __init__(
        self,
        config: dict[str, Any],
        runner: CommandRunner | None = None,
        config_object: Any | None = None,
    ):
        super().__init__(config, runner=runner, config_object=config_object)
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

    def _timer_manager(self) -> ServiceManager:
        """Return the platform's service manager for the timer's unit actions.

        Every privileged systemd action goes through it, which prefers the
        root helper socket. The deploy and webhook units set
        ``NoNewPrivileges``, so a ``sudo systemctl`` spawned from here exits 1
        and the deploy dies at "Enabling timer" — after the pull and the
        dependency install (#382).
        """
        from fraisier.service_managers import get_service_manager

        return get_service_manager(self.runner, None)

    @contextmanager
    def _helper_refusal_is_actionable(self) -> Iterator[None]:
        """Turn a helper allowlist rejection into an error naming the remedy.

        Both the action list and the service list are baked into the host's
        systemctl helper: the actions by the fraisier version installed there,
        the services by the last rendered scaffold. A host that predates #382
        (or #239, for the timer units) answers ``not allowed`` and the raw
        ``CalledProcessError`` says only that a command exited 1.
        """
        try:
            yield
        except subprocess.CalledProcessError as exc:
            reason = (exc.stderr or "").strip()
            if "not allowed" not in reason.lower():
                raise
            raise DeploymentError(
                f"systemctl helper rejected the request: {reason}. This host's "
                "helper predates the timer actions this deploy needs. Upgrade "
                "fraisier on the host, then run `fraisier scaffold && sudo "
                "fraisier scaffold-install --yes` to refresh the helper's "
                "allowlist and restart it.",
                context={
                    "fraise": self.fraise_name,
                    "environment": self.environment,
                    "unit": self.systemd_timer,
                },
                cause=exc,
            ) from exc

    def is_deployment_needed(self) -> bool:
        """Check if timer needs to be enabled/restarted.

        Uses ``systemctl is-active`` directly rather than the service manager:
        the query needs no privilege, so it works under ``NoNewPrivileges``
        with or without a helper socket, while the manager would fall back to
        ``sudo`` on a host that has none.
        """
        if not self.systemd_timer:
            return False

        try:
            result = self.runner.run(
                ["systemctl", "is-active", self.systemd_timer],
                check=False,
            )
            return result.returncode != 0
        except subprocess.CalledProcessError:  # pragma: no cover
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

                self._install_dependencies()

            # Auto-install scheduled unit files via the unit-installer
            # helper before daemon-reload (#240). Skipped if
            # the host is pre-v0.29 (no socket) UNLESS the operator explicitly
            # opted out via FRAISIER_DISABLE_WEBHOOK_AUTO_INSTALL=1.
            self._auto_install_scheduled_units_if_applicable()

            if self.systemd_timer:
                logger.info(f"Enabling timer: {self.systemd_timer}")
                manager = self._timer_manager()
                with self._helper_refusal_is_actionable():
                    manager.daemon_reload()
                    manager.enable(self.systemd_timer)
                    manager.start(self.systemd_timer)

            new_version = new_sha[:8] if new_sha else self._get_timer_state()
            return old_version, new_version

        return self._execute_with_lifecycle(_steps)

    def _auto_install_scheduled_units_if_applicable(self) -> None:
        """Run the webhook auto-install hook when wiring is available.

        Quiet no-op when:
        - The deployer wasn't given a ``config_object`` (FraisierConfig).
        - The host has no unit-installer socket (pre-v0.29; deploy_event
          records the situation but doesn't fail the deploy).
        - The env name is "unknown" (the base deployer's fallback when
          the test harness doesn't pass environment).

        Otherwise runs ``auto_install_scheduled_units`` and surfaces drift /
        pre-v0.29 errors as deploy failures so the operator sees them in
        the deploy log.
        """
        if self.config_object is None:
            return
        if self.environment == "unknown":
            return
        try:
            from fraisier.scheduled_install import (
                auto_install_scheduled_units,
                parse_auto_install_policy,
            )
        except ImportError:  # pragma: no cover
            return

        try:
            project_name = self.config_object.project_name
        except AttributeError:  # pragma: no cover
            return
        fraise_config = (
            self.config_object.fraises.get(self.fraise_name)
            if hasattr(self.config_object, "fraises")
            else None
        )
        if not fraise_config or fraise_config.get("type") != "scheduled":
            return
        env_config = (fraise_config.get("environments") or {}).get(
            self.environment
        ) or {}
        try:
            policy = parse_auto_install_policy(env_config)
        except Exception as exc:
            logger.warning(
                "auto_install policy parse failed for %s/%s: %s — skipping",
                self.fraise_name,
                self.environment,
                exc,
            )
            return

        socket_path = naming.unit_installer_socket_path(project_name, self.environment)
        is_socket_present = socket_path.is_socket()
        if not is_socket_present:
            logger.warning(
                "unit-installer socket not present at %s — "
                "skipping webhook auto-install. Run scaffold-install on the "
                "host to bootstrap.",
                socket_path,
            )
            return

        report = auto_install_scheduled_units(
            self.config_object,
            self.environment,
            fraise_name=self.fraise_name,
            policy=policy,
            socket_path=socket_path,
            is_socket_present=is_socket_present,
        )
        if report.installed:
            logger.info(
                "auto-installed %d unit(s) via helper: %s",
                len(report.installed),
                ", ".join(report.installed),
            )
        if report.drift_overwrites:
            logger.warning(
                "overwrote %d drifted unit(s) per on_drift=overwrite: %s",
                len(report.drift_overwrites),
                ", ".join(report.drift_overwrites),
            )
        if report.skipped_drift_units:
            logger.warning(
                "skipped %d drifted unit(s) per on_drift=skip: %s",
                len(report.skipped_drift_units),
                ", ".join(report.skipped_drift_units),
            )
        if report.retried_busy:
            logger.info(
                "deploy retried %d time(s) on helper-busy before succeeding",
                report.retried_busy,
            )

    def _get_timer_state(self) -> str | None:
        """Get timer active state as version proxy.

        Unprivileged, like :meth:`is_deployment_needed`.
        """
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
        except (subprocess.CalledProcessError, IndexError):  # pragma: no cover
            return None

    def health_check(self) -> bool:
        """Check if timer is active.

        Unprivileged, like :meth:`is_deployment_needed`.
        """
        if not self.systemd_timer:
            return True
        try:
            result = self.runner.run(
                ["systemctl", "is-active", self.systemd_timer],
                check=False,
            )
            return result.returncode == 0
        except subprocess.CalledProcessError:  # pragma: no cover
            return False

    def rollback(self, to_version: str | None = None) -> DeploymentResult:
        """Rollback: revert git, then restart timer."""
        start_time = time.time()
        current_version = self.get_current_version() or self._get_timer_state()
        target = to_version or self._previous_sha

        try:
            if target and self.app_path:
                logger.info(f"Rolling back git to {target[:8]}")
                self._git_rollback(target)

            if self.systemd_timer:
                logger.info(f"Restarting timer: {self.systemd_timer}")
                with self._helper_refusal_is_actionable():
                    self._timer_manager().restart(self.systemd_timer)

            new_version = target[:8] if target else self._get_timer_state()
            duration = time.time() - start_time

            self._write_status("rolled_back", commit_sha=target)
            return DeploymentResult(
                success=True,
                status=DeploymentStatus.ROLLED_BACK,
                old_version=current_version,
                new_version=new_version,
                duration_seconds=duration,
            )

        except Exception as e:  # pragma: no cover
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

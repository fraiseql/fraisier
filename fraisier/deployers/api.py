"""API fraise deployer - for web services and APIs."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fraisier.errors import DeploymentError, HealthCheckError, MigrationError

if TYPE_CHECKING:
    from fraisier.runners import CommandRunner
from fraisier.health_check import HealthCheckManager, HTTPHealthChecker
from fraisier.timeout import DeploymentTimeoutExpired, deployment_timeout

from .base import BaseDeployer, DeploymentResult, DeploymentStatus
from .mixins import GitDeployMixin

logger = logging.getLogger("fraisier")


@dataclass(frozen=True)
class RestoreOutcome:
    """What :meth:`APIDeployer._restore_previous_state` actually did.

    The failure handler needs two things to report a deploy honestly (#293):
    which status to use, and — when the database rollback failed — the incident
    text, so it does not overwrite it with the original deploy error.

    Both axes are described: the database rollback and the git revert. #293
    deliberately reported only the first, because promoting git-only reverts
    changes the status of nearly every failed deploy — that compatibility call
    was taken separately and is what #296 decided.

    ``git_reverted`` and ``service_restarted`` are claimed only when the step
    actually completed: a revert that raised leaves both false, because nothing
    was restored.
    """

    db_rollback_attempted: bool = False
    db_rollback_succeeded: bool = False
    git_reverted: bool = False
    service_restarted: bool = False
    error_message: str | None = None


class APIDeployer(GitDeployMixin, BaseDeployer):
    """Deployer for API/web service fraises.

    Handles:
    - Bare repo + worktree git operations (via GitDeployMixin)
    - Database migrations
    - Service restart via systemd
    - Health check verification
    """

    def __init__(
        self,
        config: dict[str, Any],
        runner: CommandRunner | None = None,
        config_object: Any | None = None,
    ):
        super().__init__(config, runner=runner, config_object=config_object)
        self._init_git_deploy(config)
        self.git_repo = config.get("git_repo")
        self.systemd_service = config.get("systemd_service")
        hc = config.get("health_check", {})
        self.health_check_url = hc.get("url")
        self.health_check_timeout = hc.get("timeout", 30)
        self.health_check_retries = hc.get("retries", 5)
        self.database_config = config.get("database", {})
        self.smoke_tests_config = config.get("smoke_tests")
        self.allow_irreversible = config.get("allow_irreversible", False)
        self.lock_timeout = config.get("lock_timeout", 300)
        self._migrations_applied: int = 0
        self._version_json_snapshotted = False
        self._version_json_existed = False
        self._version_json_snapshot: bytes | None = None

    def _validate_sandbox_writes(self) -> None:
        """Prove this deploy can write to the trees it is about to change (#325).

        The deploy already runs *inside* the systemd sandbox, so this needs no
        ``systemd-run``: creating and unlinking one file in each of this
        environment's ``git_repo`` and ``app_path`` answers exactly the
        question ``ProtectSystem=strict`` decides.

        A real write, deliberately, not ``os.access``. ``access(2)`` consults
        the file mode and the caller's ids; whether the mount namespace this
        process is in made the path read-only is a different question, and
        ``access`` answers the wrong one confidently.

        This is the only check that catches the class generically. The webhook
        unit installed on a host used to be one rendered for a *different*
        host, so ``ReadWritePaths=`` listed the other machine's trees and
        ``git fetch`` failed with exit 255 several steps later, with nothing
        to connect the exit code to the sandbox. It also covers what no
        template fix can reach: a hand-written or hand-edited unit.

        Paths that do not exist are skipped — a missing tree is a different
        diagnosis, made by the checks that own it.

        Raises:
            DeploymentError: naming the path, the unit and the remedy.
        """
        candidates = [
            (label, value)
            for label, value in (
                ("git_repo", self.git_repo),
                ("app_path", self.app_path),
            )
            if value
        ]

        blocked: list[str] = []
        for label, value in candidates:
            path = Path(value)
            if not path.is_dir():
                continue
            probe = path / f".fraisier-write-probe-{os.getpid()}"
            try:
                probe.touch()
            except OSError as exc:
                blocked.append(f"{label}={path} ({exc.strerror or exc})")
            else:
                probe.unlink(missing_ok=True)

        if not blocked:
            return

        project = getattr(self.config_object, "project_name", None)
        unit = f"fraisier-{project}-webhook.service" if project else "the webhook unit"
        raise DeploymentError(
            f"Cannot write to {', '.join(blocked)} from inside this deploy's "
            f"systemd sandbox. The unit running this deploy ({unit}, or the "
            f"deploy socket's unit) is ProtectSystem=strict and does not list "
            f"these paths in ReadWritePaths=, so every write below them fails "
            f"read-only — git fetch would abort with exit 255 a few steps from "
            f"here. Re-run `fraisier scaffold` and "
            f"`sudo fraisier scaffold-install --yes` on this machine to install "
            f"the unit rendered for it.",
            context={
                "fraise": self.fraise_name,
                "environment": self.environment,
                "paths": [p for _, p in candidates],
            },
        )

    def _validate_wrapper_scripts(self) -> None:
        """Validate that required wrapper scripts exist and are executable.

        Raises DeploymentError if any required wrapper script is missing or
        not executable.
        """
        errors = []

        # Check systemctl wrapper
        systemctl_wrapper = os.environ.get("FRAISIER_SYSTEMCTL_WRAPPER")
        if systemctl_wrapper:
            wrapper_path = Path(systemctl_wrapper)
            if not wrapper_path.exists():
                errors.append(
                    {
                        "wrapper": "systemctl",
                        "path": systemctl_wrapper,
                        "issue": "File not found",
                        "fix": (
                            f"sudo cp scripts/generated/systemctl-wrapper.sh "
                            f"{systemctl_wrapper}"
                        ),
                    }
                )
            elif not os.access(systemctl_wrapper, os.X_OK):
                errors.append(
                    {
                        "wrapper": "systemctl",
                        "path": systemctl_wrapper,
                        "issue": "Not executable",
                        "fix": f"sudo chmod 755 {systemctl_wrapper}",
                    }
                )

        if errors:
            error_details = {
                f"wrapper_{i + 1}": {
                    "wrapper": error["wrapper"],
                    "path": error["path"],
                    "issue": error["issue"],
                    "remediation": error["fix"],
                }
                for i, error in enumerate(errors)
            }

            msg_lines = ["Required wrapper scripts are missing or not executable:"]
            msg_lines.extend(
                f"  - {error['wrapper']}: {error['issue']} at {error['path']}"
                for error in errors
            )

            raise DeploymentError(
                "\n".join(msg_lines),
                context=error_details,
            )

    def _check_service_file_staleness_paths(self, generated: Path, live: Path) -> None:
        """Warn if *live* differs from *generated*.  Extracted for testability."""
        if not generated.exists() or not live.exists():
            return
        if generated.read_text() != live.read_text():
            logger.warning(
                "Service file is out of sync with scaffold: %s\n"
                "  Generated: %s\n"
                "  Installed: %s\n"
                "  Run 'fraisier install' to apply the updated unit.",
                self.systemd_service,
                generated,
                live,
            )

    def _check_service_file_staleness(self) -> None:
        """Warn if the installed service file differs from the scaffold-generated one.

        Compares ``{state_dir}/systemd/{service}.service`` against
        ``/etc/systemd/system/{service}.service``.  A mismatch means the server
        was not re-provisioned after a scaffold template change and the live
        service unit may contain stale configuration.

        Non-fatal: logs a WARNING and suggests running ``fraisier install``.
        """
        if not self.systemd_service:
            return

        try:
            state_dir = self._scaffold_state_dir(self._opt_config_path())
        except Exception:
            # Diagnostic only — never let a config-resolution hiccup break deploy.
            logger.debug(
                "Could not resolve state_dir for staleness check", exc_info=True
            )
            return

        generated = state_dir / "systemd" / self.systemd_service
        live = Path("/etc/systemd/system") / self.systemd_service
        self._check_service_file_staleness_paths(generated, live)

    @staticmethod
    def _opt_config_path() -> Path:
        """Path to the server-side fraises.yaml the deploy reads/writes."""
        return Path(os.environ.get("FRAISIER_CONFIG") or "/opt/fraisier/fraises.yaml")

    def _sync_config_if_needed(self) -> None:
        """Sync fraises.yaml from git checkout and regenerate scaffold if changed."""
        if not self.app_path:
            return
        opt_config = self._opt_config_path()
        app_config = Path(self.app_path) / "fraises.yaml"

        if app_config.exists():
            self._sync_fraises_yaml(source_path=app_config, dest_path=opt_config)
            if self._detect_config_changes(config_path=opt_config):
                logger.info(
                    "fraises.yaml changed — regenerating nginx and scaffold config"
                )
                self._regenerate_scaffold(config_path=opt_config)
                # Persist hash now: scaffold files on disk match the new config.
                # Saving before install means a broken install does not cause an
                # infinite regenerate+install loop on subsequent deploys (#193).
                # Trade-off: if disk fills mid-regenerate and some files are
                # corrupt, the hash is still saved and future deploys will skip
                # regeneration.  Recovery: run `fraisier scaffold` manually.
                from fraisier.config_watcher import ConfigWatcher

                ConfigWatcher(opt_config.parent).save_hash()
                logger.info("Scaffold regenerated — config hash updated")
                self._install_scaffold(config_path=opt_config)
                logger.info("Scaffold regenerated and installed")

    def _version_json_path(self) -> Path | None:
        """Path to the deployed ``version.json``, or None when app_path is unset."""
        if not self.app_path:
            return None
        return Path(self.app_path) / "version.json"

    def _snapshot_version_json(self) -> None:
        """Record version.json before it is regenerated, for abort rollback (#257).

        version.json is regenerated *before* migrations run because the rebuild
        strategy reads it to stamp ``app_version`` into the database. If the
        deploy later aborts (e.g. migrations fail), the freshly written
        version.json would otherwise be left advanced ahead of the actual
        deployed schema, making ``/health`` and ``fraisier health`` report a
        version that was never successfully deployed. Capturing the prior state
        here lets the abort paths roll version.json back via
        :meth:`_restore_version_json`.
        """
        self._version_json_snapshotted = True
        self._version_json_existed = False
        self._version_json_snapshot = None
        path = self._version_json_path()
        if path is None:
            return
        try:
            if path.exists():
                self._version_json_snapshot = path.read_bytes()
                self._version_json_existed = True
        except OSError as exc:
            logger.warning("Could not snapshot version.json before deploy: %s", exc)

    def _restore_version_json(self) -> None:
        """Restore version.json to its pre-deploy state after an aborted deploy.

        No-op unless this deploy snapshotted version.json first, so a standalone
        :meth:`rollback` invocation never deletes a live version.json. Rewrites
        the captured bytes, or removes the file when none existed before the
        deploy (issue #257).
        """
        if not self._version_json_snapshotted:
            return
        path = self._version_json_path()
        if path is None:
            return
        try:
            if self._version_json_existed and self._version_json_snapshot is not None:
                path.write_bytes(self._version_json_snapshot)
                logger.info(
                    "Restored version.json to its pre-deploy value after aborted deploy"
                )
            elif not self._version_json_existed and path.exists():
                path.unlink()
                logger.info("Removed version.json written during aborted deploy")
        except OSError as exc:
            logger.warning(
                "Could not restore version.json after aborted deploy: %s", exc
            )

    def _generate_version_json(self) -> None:
        """Write version.json to app_path from pyproject.toml + git metadata.

        Non-fatal: logs a warning and continues if anything fails (e.g. no
        pyproject.toml in the deployed project).
        """
        if not self.app_path:
            return
        from fraisier.versioning import generate_version_json, write_version

        app_dir = Path(self.app_path)
        try:
            schema_dir: Path | None = None
            if self.database_config:
                raw = self.database_config.get("migrations_dir", "db/migrations")
                candidate = Path(raw) if Path(raw).is_absolute() else app_dir / raw
                if candidate.is_dir():
                    schema_dir = candidate

            info = generate_version_json(app_dir, schema_dir=schema_dir)
            write_version(info, app_dir / "version.json")
            logger.info("Generated version.json: v%s (%s)", info.version, info.commit)
        except Exception as exc:
            logger.warning("Could not generate version.json: %s", exc)

    def _run_database_migrations(self) -> None:
        """Run database migrations, stopping the service first for rebuild."""
        if self._is_rebuild_strategy() and self.systemd_service:
            logger.info("Stopping service for rebuild: %s", self.systemd_service)
            self._stop_service()
        logger.info("Running database migrations")
        self._run_strategy()

    def _run_smoke_tests_or_halt(
        self,
        start_time: float,
        old_version: str | None,
        db_pk: int | None,
    ) -> DeploymentResult | None:
        """Run configured smoke tests post-health; return a failed result if any fail.

        Returns ``None`` when no smoke tests are configured or all pass.
        On failure: if the failing test's ``on_failure`` is ``rollback``,
        invoke the existing rollback path and return a rolled-back result.
        If ``halt``, return a failed result without rollback. ``warn``
        failures are absorbed by ``run_smoke_tests`` itself and never
        reach this method.
        """
        if not self.smoke_tests_config:
            return None
        from fraisier import smoke_tests

        # Health check URL provides the scheme+host base for relative
        # smoke_test URLs. Stripped to scheme://host (no path).
        base_url: str | None = None
        if self.health_check_url:
            from urllib.parse import urlparse

            parsed = urlparse(self.health_check_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
        tests = smoke_tests.load_smoke_tests(
            {"smoke_tests": self.smoke_tests_config, "health_check": {}},
            base_url=base_url,
        )
        if not tests:
            return None
        # resolve_and_run materializes each test's token_provider then
        # runs the smoke tests. The two failure modes have distinct
        # policies. A DeploymentError means a token_provider failed
        # (script crash, IdP 401, network error) — halt without
        # rollback, since a transient IdP hiccup is not a code
        # regression. A SmokeTestError means a probe ran but failed
        # the test's on_failure policy decides rollback vs halt.
        try:
            smoke_tests.resolve_and_run(tests)
        except DeploymentError as exc:
            duration = time.time() - start_time
            logger.warning("Token provider failed: %s", exc)
            self._write_status("failed", error_message=str(exc))
            result = DeploymentResult(
                success=False,
                status=DeploymentStatus.FAILED,
                old_version=old_version,
                duration_seconds=duration,
                error_message=str(exc),
                error=exc,
            )
            self._complete_db_record(db_pk, result)
            return result
        except smoke_tests.SmokeTestError as exc:
            duration = time.time() - start_time
            logger.warning("Smoke test failed: %s", exc)
            if exc.rollback and self._previous_sha:
                rollback_result = self.rollback()
                result = self._build_rollback_result(
                    rollback_result, old_version, duration
                )
                self._complete_db_record(db_pk, result)
                return result
            # halt: abort without rollback.
            self._write_status("failed", error_message=str(exc))
            result = DeploymentResult(
                success=False,
                status=DeploymentStatus.FAILED,
                old_version=old_version,
                duration_seconds=duration,
                error_message=str(exc),
            )
            self._complete_db_record(db_pk, result)
            return result
        return None

    def _run_post_migrate(self) -> None:
        """Run database.post_migrate SQL hooks (#204).

        Best-effort scoping: a ``halt`` step raises ``DeploymentError``
        and aborts the deploy before the service is restarted (nothing
        is yet serving the new code, so no rollback is needed). A
        ``warn`` step logs and continues. Skipped silently when the
        ``post_migrate`` list is empty or ``database_url`` is missing —
        no app DB to connect to.
        """
        from fraisier import post_migrate

        post_migrate.run_configured_post_migrate(
            self.database_config,
            app_path=Path(self.app_path) if self.app_path else Path(),
            runner=self.runner,
        )

    def _check_health_or_rollback(
        self,
        start_time: float,
        old_version: str | None,
        db_pk: int | None,
    ) -> DeploymentResult | None:
        """Check health post-deployment; roll back and return result if it fails."""
        logger.info(f"Running health check: {self.health_check_url}")
        if self._wait_for_health():
            return None
        if self._previous_sha:
            logger.warning(
                "Health check failed, rolling back to %s",
                self._previous_sha[:8],
            )
            rollback_result = self.rollback()
            duration = time.time() - start_time
            result = self._build_rollback_result(rollback_result, old_version, duration)
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

    def execute(self) -> DeploymentResult:
        """Execute API deployment."""
        start_time = time.time()
        old_version = None
        self._write_status("deploying")
        db_pk = self._start_db_record()

        timeout = self.config.get("timeout", 600)

        # Validate wrapper scripts before deployment starts
        self._validate_wrapper_scripts()

        # Prove the sandbox can write where this deploy is about to (#325),
        # before the first step that would fail on it with a bare exit code.
        self._validate_sandbox_writes()

        try:
            with deployment_timeout(timeout):
                # Config sync and scaffold regeneration (pre-pull, from cached worktree)
                try:
                    self._sync_config_if_needed()
                except Exception as e:
                    logger.error(
                        f"Pre-pull scaffold sync failed: {e}\n"
                        "nginx config may be stale — run: "
                        "fraisier scaffold && fraisier scaffold-install --yes"
                    )
                    # Continue with deployment - config mismatch is not fatal

                # Warn if the installed service file has drifted from scaffold
                self._check_service_file_staleness()

                # Step 1: Git pull via bare repo
                logger.info(f"Deploying via bare repo to {self.app_path}")
                old_sha, new_sha = self._git_pull()
                old_version = old_sha[:8] if old_sha else None

                # Step 1.5: Re-sync after checkout so a new fraises.yaml in this
                # commit is always picked up before install/migrate (issue #158).
                #
                # Fatal by design (#279): this runs after checkout, so it is the
                # step that re-bakes the install-helper allowlist when
                # install.command changed. If it fails, the units on disk no
                # longer match the deployed config and the install step would
                # hit a stale allowlist ("command not allowed"). Abort here,
                # naming the cause, instead of sailing into that masked failure.
                try:
                    self._sync_config_if_needed()
                except Exception as e:
                    raise DeploymentError(
                        "Post-pull scaffold config sync/regeneration failed "
                        "after checkout, so system units may not match the "
                        "deployed fraises.yaml. The most common cause is a "
                        "changed install.command whose install-helper allowlist "
                        "could not be re-baked (#279). Deploy aborted before the "
                        "install step to avoid a misleading 'command not "
                        "allowed'.\n"
                        "  Remediation: fix the underlying error below, then "
                        "redeploy (or run `fraisier scaffold && fraisier "
                        "scaffold-install --yes`).",
                        context={"phase": "post_pull_config_sync"},
                        cause=e,
                        # Remediation is already inline above; suppress the
                        # class-level trailing hint so it isn't duplicated with
                        # the cause's own hint when e is a DeploymentError.
                        recovery_hint="",
                    ) from e

                # Step 2: Install dependencies
                self._install_dependencies()

                # Step 2.5: Generate version.json from pyproject.toml + git
                # metadata. Snapshot first so an aborted deploy can roll
                # version.json back instead of leaving it advanced ahead of the
                # schema (issue #257). Generated before migrations because the
                # rebuild strategy reads it to stamp the database.
                self._snapshot_version_json()
                self._generate_version_json()

                # Step 3: Run database migrations via strategy if configured
                if self.database_config:
                    self._run_database_migrations()
                    self._run_post_migrate()

                # Step 4: Restart service (unless strategy handles it)
                if self.systemd_service and not self._is_restore_migrate_strategy():
                    logger.info(f"Restarting service: {self.systemd_service}")
                    self._restart_service()

                # Step 5: Health check
                if self.health_check_url:
                    early = self._check_health_or_rollback(
                        start_time, old_version, db_pk
                    )
                    if early is not None:
                        return early

                # Step 5.5: Authenticated smoke tests (#204 PR B)
                smoke = self._run_smoke_tests_or_halt(start_time, old_version, db_pk)
                if smoke is not None:
                    return smoke

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
                self._notify(result)
                return result

        except DeploymentTimeoutExpired as e:
            result = self._handle_timeout(e, old_version, start_time)
            self._complete_db_record(db_pk, result)
            return result

        except Exception as e:
            duration = time.time() - start_time
            logger.exception(f"Deployment failed: {e}")
            wrapped = self._wrap_error(e)
            outcome = self._restore_previous_state()
            self._restore_version_json()

            status, state, message = self._classify_failure(outcome, str(e))
            # Written after the restore, and with the restore's own message when
            # it has one — otherwise this overwrote the "do NOT restart" incident
            # text with the original deploy error (#293).
            self._write_status(state, error_message=message)
            result = DeploymentResult(
                success=False,
                status=status,
                old_version=old_version,
                duration_seconds=duration,
                error_message=message,
                error=wrapped,
            )
            self._complete_db_record(db_pk, result)
            self._notify(result)
            return result

    @staticmethod
    def _classify_failure(
        outcome: RestoreOutcome, deploy_error: str
    ) -> tuple[DeploymentStatus, str, str]:
        """Map a restore outcome onto (result status, status-file state, message).

        The database axis is checked first because it is the one that can leave
        the schema dirty. A git-only revert is a real rollback and reports as
        one (#296) — that changes the status of nearly every failed deploy on a
        box with history, which is why it was decided separately from #293.

        A revert that *raised* stays ``FAILED``, deliberately: it restored
        nothing, and ``ROLLBACK_FAILED`` means "the schema may be half-migrated,
        do not restart", which is false when no migrations ever ran.
        """
        if outcome.db_rollback_attempted:
            if outcome.db_rollback_succeeded:
                return (
                    DeploymentStatus.ROLLED_BACK,
                    "rolled_back",
                    f"{deploy_error} — database rolled back to the previous state",
                )
            return (
                DeploymentStatus.ROLLBACK_FAILED,
                "rollback_failed",
                outcome.error_message or deploy_error,
            )
        if outcome.git_reverted:
            restored = "reverted to the previous commit"
            if outcome.service_restarted:
                restored += " and the service restarted on it"
            return (
                DeploymentStatus.ROLLED_BACK,
                "rolled_back",
                f"{deploy_error} — {restored}",
            )
        return DeploymentStatus.FAILED, "failed", deploy_error

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
        self._restore_version_json()
        self._write_status("failed", error_message=str(exc))
        return DeploymentResult(
            success=False,
            status=DeploymentStatus.FAILED,
            old_version=old_version,
            duration_seconds=duration,
            error_message=str(exc),
        )

    def _restore_previous_state(self) -> RestoreOutcome:
        """Restore database, git, and service to previous state after a failure.

        Order matters: database first (to avoid running old code against new
        schema), then git checkout, then service restart.

        Returns what was attempted so the caller can report it (#293). A DB
        rollback that ran and succeeded, one that left the schema dirty, and a
        plain failure are three different outcomes and used to be one.
        """
        if not self._previous_sha:
            return RestoreOutcome()

        attempted = self._migrations_applied > 0 and bool(self.database_config)
        try:
            if attempted:
                db_result = self._rollback_database(None, self._previous_sha)
                if not db_result.success:
                    logger.critical(
                        "Database rollback failed during restore: %s",
                        db_result.error_message,
                    )
                    # Deliberately skip the git rollback: old code against a
                    # half-migrated schema is worse than leaving the tree.
                    return RestoreOutcome(
                        db_rollback_attempted=True,
                        error_message=db_result.error_message,
                    )
            self._git_rollback(self._previous_sha)
            restarted = bool(self.systemd_service)
            if restarted:
                self._restart_service()
        except Exception as rollback_exc:
            logger.critical("Rollback after failure also failed: %s", rollback_exc)
            return RestoreOutcome(
                db_rollback_attempted=attempted,
                error_message=f"Rollback after failure also failed: {rollback_exc}",
            )
        return RestoreOutcome(
            db_rollback_attempted=attempted,
            db_rollback_succeeded=attempted,
            git_reverted=True,
            service_restarted=restarted,
        )

    def _resolve_strategy(self) -> tuple[Any, Path, Path, str | None]:
        """Resolve database strategy, config path, migrations dir, and database_url."""
        from fraisier.strategies import get_strategy

        strategy_name = self.database_config.get("strategy", "apply")
        strategy_map = {
            "apply": "migrate",
            "rebuild": "rebuild",
            "migrate": "migrate",
            "restore_migrate": "restore_migrate",
        }
        resolved = strategy_map.get(strategy_name, strategy_name)

        kwargs: dict[str, Any] = {}
        admin_url = self.database_config.get("admin_url")
        if admin_url:
            kwargs["admin_url"] = admin_url
        if resolved == "rebuild":
            kwargs["required_roles"] = self.database_config.get("required_roles", [])
            kwargs["create_template"] = bool(
                self.database_config.get("create_template", False)
            )
            kwargs["template_name"] = self.database_config.get("template_name")
            if self.app_path:
                kwargs["project_dir"] = Path(self.app_path)
            if self.database_config.get("app_version"):
                kwargs["app_version"] = self.database_config["app_version"]
        if resolved == "migrate" and self.database_config.get("pre_migrate_dump"):
            kwargs["pre_migrate_dump"] = self.database_config["pre_migrate_dump"]
            kwargs["db_name"] = self.database_config.get("name", "")
        if resolved == "restore_migrate":
            kwargs["restore_config"] = self.database_config.get("restore", {})
            kwargs["db_name"] = self.database_config.get("name", "")
            if self.systemd_service:
                from fraisier.service_managers import get_service_manager

                kwargs["service_manager"] = get_service_manager(self.runner, None)
                kwargs["service_name"] = self.systemd_service

        strategy = get_strategy(resolved, **kwargs)
        confiture_config = Path(
            self.database_config.get("confiture_config", "confiture.yaml")
        )
        migrations_dir = Path(
            self.database_config.get("migrations_dir", "db/migrations")
        )
        database_url = self.database_config.get("database_url")
        return strategy, confiture_config, migrations_dir, database_url

    def _resolve_paths_against_app(
        self, confiture_config: Path, migrations_dir: Path
    ) -> tuple[Path, Path]:
        """Resolve relative paths against app_path.

        Raises DeploymentError if app_path is configured but does not exist.
        """
        confiture_config = Path(confiture_config)
        migrations_dir = Path(migrations_dir)

        if not self.app_path:
            return confiture_config, migrations_dir

        app_dir = Path(self.app_path)
        if not app_dir.is_dir():
            raise DeploymentError(
                f"app_path does not exist: {self.app_path}",
                context={
                    "fraise": self.fraise_name,
                    "environment": self.environment,
                },
            )

        if not confiture_config.is_absolute():
            confiture_config = app_dir / confiture_config
        if not migrations_dir.is_absolute():
            migrations_dir = app_dir / migrations_dir

        return confiture_config, migrations_dir

    def _run_strategy(self) -> None:
        """Run database migrations via deployment strategy.

        Resolves relative paths (confiture_config, migrations_dir) against
        app_path explicitly, and also changes cwd so that any internal
        relative path resolution inside confiture works correctly.
        """
        strategy, confiture_config, migrations_dir, database_url = (
            self._resolve_strategy()
        )
        confiture_config, migrations_dir = self._resolve_paths_against_app(
            confiture_config, migrations_dir
        )

        pre_verify = self.database_config.get("pre_migrate_verify", False)
        hooks_config = self.database_config.get("hooks")
        old_cwd = Path.cwd()
        app_dir = Path(self.app_path) if self.app_path else None
        if app_dir and app_dir.is_dir():
            os.chdir(app_dir)
        try:
            result = strategy.execute(
                confiture_config,
                migrations_dir=migrations_dir,
                allow_irreversible=self.allow_irreversible,
                pre_migrate_verify=pre_verify,
                database_url=database_url,
                hooks_config=hooks_config,
            )
        except MigrationError as exc:
            # A batch that fails part-way has still applied — and committed —
            # everything before the failure. Record that count before the
            # exception escapes, or `_restore_previous_state` sees 0 and skips
            # the DB rollback, leaving the schema half-migrated (#272).
            # `steps_applied is None` means the count is unknown; stay at 0 and
            # skip, rather than rolling back a guessed number of migrations.
            if exc.steps_applied:
                self._migrations_applied = exc.steps_applied
            raise
        finally:
            os.chdir(old_cwd)

        self._migrations_applied = result.migrations_applied

        if not result.success:
            raise DeploymentError(
                "; ".join(result.errors) or "Database migration failed",
                context={
                    "strategy": strategy.__class__.__name__,
                    "fraise": self.fraise_name,
                },
            )

    def _is_rebuild_strategy(self) -> bool:
        """Check if the configured strategy is rebuild."""
        return self.database_config.get("strategy") == "rebuild"

    def _is_restore_migrate_strategy(self) -> bool:
        """Check if the configured strategy is restore_migrate."""
        return self.database_config.get("strategy") == "restore_migrate"

    def _stop_service(self) -> None:
        """Stop service."""
        if not self.systemd_service:
            return
        from fraisier.service_managers import get_service_manager

        service_manager = get_service_manager(self.runner, None)
        service_manager.stop(self.systemd_service)

    def _restart_service(self) -> None:
        """Restart service."""
        if not self.systemd_service:
            return
        from fraisier.service_managers import get_service_manager

        service_manager = get_service_manager(self.runner, None)
        service_manager.restart(self.systemd_service)

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
    ) -> DeploymentResult:
        """Roll back database migrations. Returns failure result or None."""
        strategy, confiture_config, migrations_dir, database_url = (
            self._resolve_strategy()
        )
        confiture_config, migrations_dir = self._resolve_paths_against_app(
            confiture_config, migrations_dir
        )
        hooks_config = self.database_config.get("hooks")
        db_result = strategy.rollback(
            confiture_config,
            migrations_dir=migrations_dir,
            steps=self._migrations_applied,
            database_url=database_url,
            hooks_config=hooks_config,
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

            # Revert version.json so a rolled-back deploy never reports the
            # version it failed to ship (issue #257). Gated on this run having
            # snapshotted, so a standalone rollback leaves version.json alone.
            self._restore_version_json()

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

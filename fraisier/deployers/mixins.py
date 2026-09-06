"""Shared deployer mixins for common deployment patterns."""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fraisier.errors import DeploymentError, FrameworkError
from fraisier.git.operations import (
    clone_bare_repo,
    fetch_and_checkout,
    get_worktree_sha,
)
from fraisier.status import DeploymentStatusFile, current_owner, write_status

if TYPE_CHECKING:
    from collections.abc import Callable

    from fraisier.deployers.base import DeploymentResult
    from fraisier.runners import CommandRunner

logger = logging.getLogger("fraisier")

DEFAULT_REPOS_BASE = Path("/var/lib/fraisier/repos")


def _install_failure_advice(
    stderr: str, *, app_path: str, via_socket: bool = False
) -> str | None:
    """Derive operator advice from an install command's captured ``stderr``.

    Returns ``None`` when no known signature matches. The ``__pycache__``
    signature is checked first (most specific); the generic
    ``.cache``/``Permission denied`` signature is the HOME-cache hint that
    points at the ``sudo -H`` fix for the sudo fallback (#276).

    ``via_socket=True`` reshapes the ``.cache`` hint: the install-helper
    socket already runs as the install user with a correct HOME by
    construction, so a cache-write denial there is a genuinely unwritable
    cache dir — never an unset HOME — and must not suggest ``sudo -H``.
    """
    if "__pycache__" in stderr and "Permission denied" in stderr:
        return (
            "Foreign-owned __pycache__ directories are blocking uv sync."
            # Not `-user root`: the writer can be service.user (#292) or
            # install.user (#286). Resolve the venv's owner at paste time so
            # the command is right whichever identity actually owns it (#303).
            f" Fix: sudo find {app_path}/.venv -name __pycache__"
            f' ! -user "$(stat -c %U {app_path}/.venv)"'
            " -type d -exec rm -rf {} +"
            " then retry the deployment."
            " The venv may be corrupted — run uv sync --frozen"
            " manually after cleanup."
            " See: https://github.com/fraiseql/fraisier/issues/196"
        )
    if ".cache" in stderr and "Permission denied" in stderr:
        if via_socket:
            return (
                "A tool (uv/pip/cargo) could not write its cache directory."
                " The install user's cache path (e.g. ~/.cache) is not"
                " writable — check ownership and that the filesystem is not"
                " full or mounted read-only."
            )
        return (
            "The install user's HOME may be unset or unwritable, so a tool"
            " (uv/pip/cargo) could not create its cache. Ensure the fallback"
            " uses `sudo -H` or run via the install-helper socket."
            " See: https://github.com/fraiseql/fraisier/issues/276"
        )
    return None


class StatusRecordMixin:
    """Writing the deployment record.

    Split out of :class:`GitDeployMixin` so a deployer that does not use the
    git deploy flow still owns its record: nothing else writes it any more, so
    a deployer without this wrote nothing at all (#378).
    """

    # Provided by BaseDeployer (the consuming class).
    fraise_name: str
    environment: str
    status_dir: Path

    def _write_status(self, state: str, **kwargs: Any) -> None:
        """Write deployment status file."""
        # Stamped on every write, not only the first: the record must always
        # name a process a reader can look for, so a deploy killed mid-flight is
        # distinguishable from one still running (#349).
        status = DeploymentStatusFile(
            fraise_name=self.fraise_name,
            environment=self.environment,
            state=state,
            **current_owner(),
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


class GitDeployMixin(StatusRecordMixin):
    """Mixin providing bare-repo git operations and status file writing.

    Expects the consuming class to set self.fraise_name and self.environment
    (typically via BaseDeployer.__init__).
    """

    # These attributes are provided by BaseDeployer (the consuming class).
    runner: CommandRunner
    config: dict[str, Any]

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
        # Pass the bare repo: the worktree has no .git in fraisier's
        # deploy model, so the in-worktree read always fails (#321).
        sha = get_worktree_sha(Path(self.app_path), bare_repo=self.bare_repo)
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
        assert self.app_path is not None, (
            "app_path must be set before calling _git_pull"
        )
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
            # Try socket helper first — avoids sudo, works with NoNewPrivileges=true
            safe_fraise = self.fraise_name.upper().replace("-", "_")
            safe_env = self.environment.upper().replace("-", "_")
            env_key = f"FRAISIER_INSTALL_SOCKET_{safe_fraise}_{safe_env}"
            socket_path = os.environ.get(env_key)
            if socket_path and Path(socket_path).exists():
                logger.info(
                    "Installing dependencies via socket %s: %s", socket_path, cmd
                )
                self._install_via_socket(socket_path, cmd, self.app_path)
                return
            # Fallback to sudo -u (e.g. first bootstrap before socket unit is installed)
            logger.debug(
                "Install helper socket not available (%s), falling back to sudo -u",
                env_key,
            )
            # Verify manifest ownership and delete paths owned by wrong user
            _co = getattr(self, "config_object", None)
            if _co is not None:
                from fraisier.config.loader import FraisierConfig
                from fraisier.deployers.preflight_ownership import (
                    _verify_manifest_ownership,
                )
                from fraisier.manifest import build_manifest

                if isinstance(_co, FraisierConfig):
                    manifest = build_manifest(_co)
                    _verify_manifest_ownership(manifest)
            # Resolve the binary to an absolute path so sudo can find it
            # even though sudo resets PATH (uv is typically per-user).
            resolved = shutil.which(cmd[0])
            if resolved:
                cmd[0] = resolved
            # -H sets HOME to the target user's home so HOME-writing tools
            # (uv/pip/cargo/npm) can create their caches instead of failing on
            # the invoker's /root/.cache. -H (not -i): no login shell, cwd stays
            # app_path. See: https://github.com/fraiseql/fraisier/issues/276
            cmd = ["sudo", "-H", "-u", self.install_user, *cmd]
        logger.info("Installing dependencies: %s", cmd)
        try:
            self.runner.run(cmd, cwd=self.app_path)
        except subprocess.CalledProcessError as exc:
            suggested = f"cd {self.app_path} && {shlex.join(cmd)}"
            stderr = exc.stderr or ""
            advice = _install_failure_advice(stderr, app_path=self.app_path)
            msg = (
                f"Install command failed (exit code {exc.returncode}): "
                f"{shlex.join(exc.cmd) if isinstance(exc.cmd, list) else exc.cmd}\n"
                f"  Directory: {self.app_path}\n"
                f"  To debug: {suggested}"
            )
            # Surface the child's stderr tail so the real cause is visible in
            # the deploy journal without re-running the command by hand (#277).
            # Bounded to the last 2000 chars so a noisy build log can't flood
            # the journal. Note: this folds stderr into the DeploymentError
            # message, which is persisted as status.error_message and returned
            # by the authenticated /api/status/<fraise>/details endpoint — a
            # wider (token-gated, bounded) surface than the un-persisted
            # context dict alone.
            if stderr.strip():
                msg += f"\n  stderr: {stderr.strip()[-2000:]}"
            if advice:
                msg += f"\n  Advice: {advice}"
            raise DeploymentError(
                msg,
                context={
                    "command": exc.cmd,
                    "cwd": self.app_path,
                    "exit_code": exc.returncode,
                    "stdout": exc.stdout or "",
                    "stderr": stderr,
                    "suggested_command": suggested,
                    "advice": advice,
                },
            ) from exc

    def _install_via_socket(
        self, socket_path: str, command: list[str], cwd: str
    ) -> None:
        """Run *command* in *cwd* via the install-helper Unix socket.

        Raises:
            DeploymentError: If the connection fails or the command exits non-zero.
        """
        request = json.dumps({"command": command, "cwd": cwd}).encode() + b"\n"
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            with sock:
                sock.connect(socket_path)
                sock.sendall(request)
                sock.shutdown(socket.SHUT_WR)
                raw = b""
                buf = bytearray()
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if b"\n" in buf:
                        raw = bytes(buf.split(b"\n", 1)[0])
                        break
        except OSError as exc:
            raise DeploymentError(
                f"Failed to connect to install helper socket {socket_path}: {exc}",
                context={"socket_path": socket_path, "command": command, "cwd": cwd},
            ) from exc

        try:
            response = json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DeploymentError(
                f"Invalid response from install helper: {exc}",
                context={
                    "socket_path": socket_path,
                    "raw": raw.decode(errors="replace"),
                },
            ) from exc

        if not response.get("ok"):
            error = response.get("error") or response.get("stderr") or "unknown error"
            suggested = f"cd {cwd} && {shlex.join(command)}"
            # Prefer advice the helper supplied; otherwise derive it from the
            # returned stderr. Socket variant: no `sudo -H` hint (#277).
            advice = response.get("advice") or _install_failure_advice(
                response.get("stderr") or "", app_path=cwd, via_socket=True
            )
            msg = (
                "Install command failed"
                f" (exit code {response.get('returncode', '?')}):"
                f" {shlex.join(command)}\n"
                f"  Directory: {cwd}\n"
                f"  Error: {error}\n"
                f"  To debug: {suggested}"
            )
            if advice:
                msg += f"\n  Advice: {advice}"
            raise DeploymentError(
                msg,
                context={
                    "command": command,
                    "cwd": cwd,
                    "exit_code": response.get("returncode"),
                    "stdout": response.get("stdout", ""),
                    "stderr": response.get("stderr", ""),
                    "suggested_command": suggested,
                    "advice": advice,
                },
            )

    def _git_rollback(
        self,
        target: str,
        runner: Any | None = None,
    ) -> None:
        """Rollback worktree to *target* SHA via bare repo checkout."""
        r = runner or self.runner
        assert self.app_path is not None, (
            "app_path must be set before calling _git_rollback"
        )
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

    def _wrap_error(self, exc: Exception) -> FrameworkError:
        """Wrap a bare exception into a structured FrameworkError."""
        ctx = {"fraise": self.fraise_name, "environment": self.environment}
        if isinstance(exc, FrameworkError):
            exc.context.update(ctx)
            return exc
        return DeploymentError(str(exc), context=ctx, cause=exc)

    def _init_notifications(self, config: dict[str, Any]) -> None:
        """Initialize notification dispatcher and lifecycle hooks."""
        from fraisier.hooks.dispatcher import build_hook_runner
        from fraisier.notifications.dispatcher import NotificationDispatcher

        notifications_config = config.get("notifications", {})
        if notifications_config:
            self._dispatcher = NotificationDispatcher.from_config(notifications_config)
        else:
            self._dispatcher = NotificationDispatcher()

        self._hook_runner = build_hook_runner(config)

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
            database_config = getattr(self, "database_config", None) or {}
            strategy = database_config.get("strategy")
            database_url = database_config.get("database_url")
            db_size_mb: int | None = None
            if database_url and result.success:
                try:
                    from fraisier.dbops.sizing import query_database_size_mb

                    db_size_mb = query_database_size_mb(
                        database_url, runner=self.runner
                    )
                except Exception:
                    # Defence-in-depth: the helper is already best-effort,
                    # but never let a sizing failure block the recorder.
                    logger.debug(
                        "query_database_size_mb raised; leaving db_size_mb=None",
                        exc_info=True,
                    )
                    db_size_mb = None
            db.complete_deployment(
                deployment_id=deployment_pk,
                success=result.success,
                new_version=result.new_version,
                error_message=result.error_message,
                strategy=strategy,
                db_size_mb=db_size_mb,
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

    def _run_hooks(self, phase: str, **kwargs: Any) -> None:
        """Run lifecycle hooks for a phase. Swallows import errors."""
        from fraisier.hooks.base import HookContext, HookPhase

        phase_enum = HookPhase(phase)
        context = HookContext(
            fraise_name=self.fraise_name,
            environment=self.environment,
            phase=phase_enum,
            config=self.config,
            **kwargs,
        )
        self._hook_runner.run(phase_enum, context)

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
        from fraisier.hooks.base import HookAbortError

        start_time = time.time()
        self._write_status("deploying")
        db_pk = self._start_db_record()

        # Pre-deploy hooks (backup, etc.) — failure aborts deployment
        try:
            self._run_hooks("before_deploy")
        except HookAbortError as e:
            duration = time.time() - start_time
            logger.error("Pre-deploy hook aborted deployment: %s", e)
            self._write_status("failed", error_message=str(e))
            result = DeploymentResult(
                success=False,
                status=DeploymentStatus.FAILED,
                duration_seconds=duration,
                error_message=str(e),
                error=self._wrap_error(e),
            )
            self._complete_db_record(db_pk, result)
            self._notify(result)
            return result

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
            self._run_hooks(
                "after_deploy",
                old_version=old_version,
                new_version=new_version,
            )
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
            self._run_hooks("on_failure", error_message=str(e))
            self._notify(result)
            return result

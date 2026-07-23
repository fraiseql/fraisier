"""Base deployer interface for all fraise types."""

from __future__ import annotations

import functools
import json
import logging
import os
import shutil
import socket as _socket_mod
import sys
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator

    from fraisier.errors import FrameworkError
    from fraisier.runners import CommandRunner

logger = logging.getLogger("fraisier")


def _get_scaffold_socket_path(project_name: str) -> str:
    """Return the Unix socket path for the scaffold-install-helper of *project_name*."""
    return f"/run/fraisier/scaffold-install-{project_name}.sock"


def _is_executable_file(path: Path) -> bool:
    """True iff *path* is a regular file (or a symlink to one) with +x set.

    Stricter than :meth:`Path.exists`: rejects directories, sockets, broken
    symlinks, and non-executable text files so the deploy daemon never
    hands the runner a path that would later fail :func:`os.execvp`.
    """
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _iter_candidates() -> Iterator[tuple[str, Path | None]]:
    """Yield (label, candidate) probes lazily, most-correct first.

    Lazy iteration matters: the happy path (sys.executable sibling) must
    short-circuit before $PATH is consulted. Otherwise the daemon stats
    every PATH entry on every deploy even when the answer is one stat
    call away.
    """
    # 1. sibling of the running Python (strongest correctness guarantee)
    if sys.executable:
        yield "sys.executable sibling", Path(sys.executable).parent / "fraisier"
    else:
        # Frozen / embedded interpreters may leave sys.executable empty.
        yield "sys.executable sibling", None

    # 2. $PATH lookup
    which_path = shutil.which("fraisier")
    yield "$PATH", Path(which_path) if which_path else None

    # 3. Hardcoded fallbacks. Home-anchored entries first so the daemon's
    #    actual install (under uv tool) is preferred over machine-wide
    #    leftovers when both happen to exist.
    try:
        home: Path | None = Path.home()
    except (RuntimeError, KeyError):
        home = None
    if home is not None:
        yield "~/.local/bin/fraisier", home / ".local" / "bin" / "fraisier"
        yield (
            "~/.local/share/uv/tools/fraisier/bin/fraisier",
            home
            / ".local"
            / "share"
            / "uv"
            / "tools"
            / "fraisier"
            / "bin"
            / "fraisier",
        )
    yield "/usr/local/bin/fraisier", Path("/usr/local/bin/fraisier")
    yield "/usr/bin/fraisier", Path("/usr/bin/fraisier")
    yield "/opt/fraisier/bin/fraisier", Path("/opt/fraisier/bin/fraisier")


@functools.cache
def _resolve_fraisier_executable() -> str:
    """Locate the fraisier executable across supported install layouts.

    Layered resolution, most-correct first:

    1. **sys.executable sibling** — the console script next to the running
       Python. By construction it is the same install as the deploy daemon
       itself, so for any standard layout (``uv tool install``, venv, pipx,
       system packaging) this is correct without further lookup.
    2. **$PATH** via :func:`shutil.which` — covers daemons launched through
       a wrapper that uses a different Python (e.g. ``python -m fraisier``
       from outside the venv).
    3. **Hardcoded fallbacks** — ``~/.local/bin/fraisier`` and
       ``~/.local/share/uv/tools/fraisier/bin/fraisier`` (the ``uv tool
       install`` layout — see issue #216), then the historical
       ``/usr/local/bin``, ``/usr/bin``, ``/opt/fraisier/bin`` paths.

    A candidate must be a regular file (or symlink to one) with the
    executable bit set; missing files, directories, broken symlinks, and
    non-executable matches are skipped.

    The result is cached for the lifetime of the process. Operationally,
    the deploy daemon restarts after every self-upgrade, so the cache
    cannot outlive a relocation of the binary it points at.

    Returns:
        Absolute path to the fraisier executable.

    Raises:
        RuntimeError: With every probed location plus a remediation hint,
            so the journald entry is self-diagnosing — operators do not
            need to re-run with ``-v`` or instrument the daemon to know
            which strategy failed.
    """
    tried: list[tuple[str, Path | None]] = []
    for source, candidate in _iter_candidates():
        tried.append((source, candidate))
        if candidate is None or not _is_executable_file(candidate):
            continue
        if source == "sys.executable sibling":
            logger.debug("Resolved fraisier executable via %s: %s", source, candidate)
        else:
            # Falling back past the sibling means the deploy daemon's Python
            # does not own a fraisier console script. The deploy still works,
            # but a `uv tool install fraisier` re-run will bring them back
            # into lockstep — log once per process (the cache pins this) so
            # operators see it without having to instrument anything.
            logger.info(
                "fraisier executable resolved via %s (%s); the deploy "
                "daemon's sys.executable=%r has no fraisier sibling. "
                "Run `uv tool install fraisier` to align them.",
                source,
                candidate,
                sys.executable,
            )
        return str(candidate)

    rendered = "\n  ".join(
        f"- {source}: {candidate if candidate is not None else '(skipped)'}"
        for source, candidate in tried
    )
    raise RuntimeError(
        "fraisier executable not found.\n"
        f"Probed locations (none were an executable regular file):\n  {rendered}\n\n"
        "Remediation:\n"
        "  - Recommended: re-install with `uv tool install fraisier` and "
        "start the deploy daemon under the same user, so its sys.executable "
        "sits next to the fraisier console script.\n"
        "  - Workaround: symlink the binary into /usr/local/bin/fraisier.\n"
        f"  - Current sys.executable: {sys.executable or '(unset)'}"
    )


class DeploymentStatus(Enum):
    """Deployment status values."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


@dataclass
class DeploymentResult:
    """Result of a deployment operation."""

    success: bool
    status: DeploymentStatus
    old_version: str | None = None
    new_version: str | None = None
    duration_seconds: float = 0.0
    error_message: str | None = None
    error: FrameworkError | None = None
    details: dict[str, Any] = field(default_factory=dict)


class BaseDeployer(ABC):
    """Abstract base class for fraise deployers.

    Each fraise type (api, etl, scheduled, backup) has its own deployer
    that implements this interface.
    """

    def __init__(
        self,
        config: dict[str, Any],
        runner: CommandRunner | None = None,
        config_object: Any | None = None,
    ):
        """Initialize deployer with fraise configuration.

        Args:
            config: Fraise configuration from fraises.yaml
            runner: Optional CommandRunner for executing shell commands.
                Defaults to LocalRunner (local subprocess execution).
            config_object: Optional typed FraisierConfig for manifest operations.
        """
        from fraisier.runners import LocalRunner

        self.config = config
        self.config_object = config_object
        self.fraise_name = config.get("fraise_name", "unknown")
        self.environment = config.get("environment", "unknown")
        self.runner = runner or LocalRunner()

    @abstractmethod
    def get_current_version(self) -> str | None:
        """Get the currently deployed version.

        Returns:
            Version string or None if not deployed
        """
        pass  # pragma: no cover

    @abstractmethod
    def get_latest_version(self) -> str | None:
        """Get the latest available version (e.g., from git).

        Returns:
            Version string or None if unavailable
        """
        pass  # pragma: no cover

    def is_deployment_needed(self) -> bool:
        """Check if deployment is needed (versions differ).

        Returns:
            True if deployment should proceed
        """
        current = self.get_current_version()
        latest = self.get_latest_version()

        if current is None or latest is None:
            return True

        return current != latest

    @abstractmethod
    def execute(self) -> DeploymentResult:
        """Execute the deployment.

        Returns:
            DeploymentResult with success/failure status
        """
        pass  # pragma: no cover

    def rollback(self, to_version: str | None = None) -> DeploymentResult:
        """Rollback to a previous version.

        Args:
            to_version: Specific version to rollback to, or previous if None

        Returns:
            DeploymentResult with rollback status
        """
        # Default implementation - subclasses can override
        return DeploymentResult(  # pragma: no cover
            success=False,
            status=DeploymentStatus.FAILED,
            error_message="Rollback not implemented for this fraise type",
        )

    def health_check(self) -> bool:
        """Check if the fraise is healthy after deployment.

        Returns:
            True if healthy
        """
        # Default implementation - subclasses can override
        return True  # pragma: no cover

    def _sync_fraises_yaml(
        self, source_path: Path | None = None, dest_path: Path | None = None
    ) -> None:
        """Pull fraises.yaml from git repository to server.

        Updates config file from source (app_path) to destination.

        Args:
            source_path: Source file path (usually from app checkout)
            dest_path: Destination file path (usually /opt/project/fraises.yaml)

        Raises:
            FileNotFoundError: If source file doesn't exist
        """

        if not source_path or not dest_path:
            logger.warning(
                "No source/dest paths configured, skipping fraises.yaml sync"
            )
            return

        source_path = Path(source_path)
        dest_path = Path(dest_path)

        logger.info(
            "Syncing fraises.yaml",
            extra={
                "event": "config_sync_started",
                "source": str(source_path),
                "destination": str(dest_path),
                "fraise": self.fraise_name,
                "environment": self.environment,
            },
        )

        # Verify source exists
        if not source_path.exists():
            raise FileNotFoundError(
                f"fraises.yaml not found at {source_path}. "
                "Ensure it's committed to git and checked out."
            )

        # Ensure destination directory exists before copying
        self.runner.run(["mkdir", "-p", str(dest_path.parent)])

        # Write atomically: copy to a temp file in the destination directory,
        # then rename onto the live path. A reader (the mtime-based config
        # auto-reload, #278) never sees a torn file. The temp name is
        # pid-unique because several fraises can sync to the same shared
        # /opt/fraisier/fraises.yaml concurrently (the deploy lock is
        # per-fraise), and a fixed temp name would let their copies collide.
        tmp_path = dest_path.with_name(f"{dest_path.name}.tmp.{os.getpid()}")
        self.runner.run(["cp", str(source_path), str(tmp_path)])
        self.runner.run(["mv", "-f", str(tmp_path), str(dest_path)])

        logger.info(
            "Config synced successfully",
            extra={
                "event": "config_sync_completed",
                "source": str(source_path),
                "destination": str(dest_path),
                "fraise": self.fraise_name,
                "environment": self.environment,
            },
        )

    def _detect_config_changes(self, config_path: Path | None = None) -> bool:
        """Detect if fraises.yaml has changed.

        Compares hash of old config vs. newly synced config.

        Args:
            config_path: Path to fraises.yaml to check

        Returns:
            True if config changed, False if identical
        """
        from fraisier.config_watcher import ConfigWatcher

        if not config_path:
            logger.warning("No config path provided, assuming no change")
            return False

        config_path = Path(config_path)
        project_dir = config_path.parent

        try:
            watcher = ConfigWatcher(project_dir)
            changed = watcher.has_changed()

            log_data = {
                "event": "config_change_detection_completed",
                "config_changed": changed,
                "config_path": str(config_path),
                "fraise": self.fraise_name,
                "environment": self.environment,
            }

            if changed:
                logger.info("Config changed, will regenerate scaffold", extra=log_data)
            else:
                logger.info("Config unchanged", extra=log_data)

            return changed
        except Exception as e:  # pragma: no cover
            logger.warning(
                f"Could not detect config changes: {e}",
                extra={
                    "event": "config_change_detection_failed",
                    "error": str(e),
                    "fraise": self.fraise_name,
                    "environment": self.environment,
                },
            )
            return True  # Assume changed if we can't detect

    @staticmethod
    def _get_fraisier_executable() -> str:
        """Return the absolute path to the fraisier executable.

        Delegates to the module-level cached resolver. See
        :func:`_resolve_fraisier_executable` for the resolution order and
        the diagnostic raised on failure.
        """
        return _resolve_fraisier_executable()

    def _regenerate_scaffold(self, config_path: Path | None = None) -> None:
        """Regenerate scaffold files based on current fraises.yaml.

        Runs 'fraisier scaffold' on the server to generate updated
        systemd units, nginx configs, sudoers rules, etc.

        Args:
            config_path: Path to fraises.yaml to use for generation

        Raises:
            DeploymentError: If scaffold regeneration fails
        """
        from fraisier.errors import DeploymentError

        if not config_path:
            logger.warning("No config path provided, skipping scaffold regeneration")
            return

        logger.info("Regenerating scaffold files")

        config_path = Path(config_path)
        project_dir = config_path.parent
        fraisier_exe = self._get_fraisier_executable()

        result = self.runner.run(
            [fraisier_exe, "-c", str(config_path), "scaffold"],
            cwd=str(project_dir),
        )

        if result.returncode != 0:
            raise DeploymentError(
                f"Failed to regenerate scaffold files: {result.stdout}"
            )

        logger.info("✓ Scaffold files regenerated")

    def _try_scaffold_install_via_socket(
        self, config_path: Path
    ) -> types.SimpleNamespace | None:
        """Attempt scaffold installation via the helper socket.

        Returns a result namespace if the socket was reachable (success or failure),
        or None if the socket is not available (caller should fall back to subprocess).
        """
        from fraisier.config import get_config

        try:
            project_name = get_config(config_path).project_name
        except Exception:
            logger.debug(
                "Could not parse config for socket path resolution, "
                "falling back to subprocess",
                exc_info=True,
            )
            return None

        socket_path = _get_scaffold_socket_path(project_name)

        try:
            with _socket_mod.socket(
                _socket_mod.AF_UNIX, _socket_mod.SOCK_STREAM
            ) as sock:
                sock.connect(socket_path)
                sock.sendall(b'{"action": "install"}\n')
                with sock.makefile("rb") as f:
                    raw = f.readline()
            response = json.loads(raw.decode())
            return types.SimpleNamespace(
                returncode=0 if response.get("ok") else 1,
                stdout=response.get("stdout", ""),
                stderr=response.get("stderr", ""),
            )
        except (
            FileNotFoundError,
            ConnectionRefusedError,
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ):
            return None

    def _install_scaffold(self, config_path: Path | None = None) -> None:
        """Install updated scaffold files to system locations.

        Tries the scaffold-install-helper Unix socket first (compatible with
        ``NoNewPrivileges=true``), then falls back to a subprocess call for
        deployments that have not yet reinstalled the scaffold.

        Args:
            config_path: Path to fraises.yaml; when provided, the subprocess
                is run with ``cd <project_dir>`` so relative paths in the
                config (e.g. ``output_dir``) resolve correctly.

        Raises:
            DeploymentError: If scaffold installation fails
        """
        from fraisier.errors import DeploymentError

        logger.info("Installing updated scaffold files")

        if config_path:
            config_path = Path(config_path)
            project_dir = config_path.parent

            # Try socket helper first (compatible with NoNewPrivileges=true)
            socket_result = self._try_scaffold_install_via_socket(config_path)
            if socket_result is not None:
                if socket_result.returncode != 0:
                    raise DeploymentError(
                        f"Failed to install scaffold files via socket helper: "
                        f"{socket_result.stdout}"
                    )
                logger.info("✓ Scaffold files installed via helper socket")
                return

            # Fall back to subprocess (pre-helper deployments)
            logger.debug(
                "Scaffold-install-helper socket not available;"
                " falling back to subprocess"
            )
            fraisier_exe = self._get_fraisier_executable()
            result = self.runner.run(
                [fraisier_exe, "-c", str(config_path), "scaffold-install", "--yes"],
                cwd=str(project_dir),
            )
        else:
            logger.warning(
                "No config_path provided to _install_scaffold(); CWD may be wrong"
            )
            fraisier_exe = self._get_fraisier_executable()
            result = self.runner.run([fraisier_exe, "scaffold-install", "--yes"])

        if result.returncode != 0:
            raise DeploymentError(f"Failed to install scaffold files: {result.stdout}")

        logger.info("✓ Scaffold files installed")

    def _rollback_config(self, config_path: Path | None = None) -> bool:
        """Rollback to previous fraises.yaml and regenerate scaffold.

        Restores previous commit version of fraises.yaml from git and
        regenerates scaffold files from the restored config.

        Args:
            config_path: Path to fraises.yaml to restore

        Returns:
            True if rollback successful, False otherwise
        """
        if not config_path:
            logger.warning("No config path provided, skipping config rollback")
            return True

        try:
            logger.info("Rolling back to previous configuration")

            # Restore previous commit of fraises.yaml from git
            config_path = Path(config_path)
            app_path = config_path.parent.parent / "app"  # Assume git checkout

            result = self.runner.run(
                ["git", "-C", str(app_path), "checkout", "HEAD~1", "--", "fraises.yaml"]
            )

            if result.returncode == 0:
                # Re-sync the previous version to server
                self._sync_fraises_yaml(
                    source_path=app_path / "fraises.yaml", dest_path=config_path
                )

                # Regenerate and install scaffold from restored config
                self._regenerate_scaffold(config_path=config_path)
                self._install_scaffold(config_path=config_path)

                logger.info("✓ Configuration rolled back")
                return True
            else:
                logger.warning("Could not restore previous config: %s", result.stdout)
                return False
        except Exception as e:
            logger.error("Config rollback failed: %s", e)
            return False

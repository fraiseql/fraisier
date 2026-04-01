"""Base deployer interface for all fraise types."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fraisier.errors import FraisierError
    from fraisier.runners import CommandRunner

logger = logging.getLogger("fraisier")


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
    error: FraisierError | None = None
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
    ):
        """Initialize deployer with fraise configuration.

        Args:
            config: Fraise configuration from fraises.yaml
            runner: Optional CommandRunner for executing shell commands.
                Defaults to LocalRunner (local subprocess execution).
        """
        from fraisier.runners import LocalRunner

        self.config = config
        self.fraise_name = config.get("fraise_name", "unknown")
        self.environment = config.get("environment", "unknown")
        self.runner = runner or LocalRunner()

    @abstractmethod
    def get_current_version(self) -> str | None:
        """Get the currently deployed version.

        Returns:
            Version string or None if not deployed
        """
        pass

    @abstractmethod
    def get_latest_version(self) -> str | None:
        """Get the latest available version (e.g., from git).

        Returns:
            Version string or None if unavailable
        """
        pass

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
        pass

    def rollback(self, to_version: str | None = None) -> DeploymentResult:
        """Rollback to a previous version.

        Args:
            to_version: Specific version to rollback to, or previous if None

        Returns:
            DeploymentResult with rollback status
        """
        # Default implementation - subclasses can override
        return DeploymentResult(
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
        return True

    # Phase 2: Config sync methods
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

        logger.info(f"Syncing fraises.yaml from {source_path}")

        # Verify source exists
        if not source_path.exists():
            raise FileNotFoundError(
                f"fraises.yaml not found at {source_path}. "
                "Ensure it's committed to git and checked out."
            )

        # Copy file
        self.runner.run(
            f"cp {source_path} {dest_path}",
            description=f"Sync fraises.yaml from {source_path}",
        )

        logger.info(f"✓ Synced fraises.yaml to {dest_path}")

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

            if changed:
                logger.info("Config changed, will regenerate scaffold")
            else:
                logger.info("Config unchanged")

            return changed
        except Exception as e:
            logger.warning(f"Could not detect config changes: {e}")
            return True  # Assume changed if we can't detect

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

        # Run scaffold regeneration
        result = self.runner.run(
            f"cd {project_dir} && fraisier -c {config_path} scaffold",
            description="Regenerate scaffold files from fraises.yaml",
        )

        if not result.ok:
            raise DeploymentError(
                f"Failed to regenerate scaffold files: {result.output}"
            )

        logger.info("✓ Scaffold files regenerated")

    def _install_scaffold(self) -> None:
        """Install updated scaffold files to system locations.

        Runs 'fraisier scaffold-install' on the server to install
        sudoers, systemd units, nginx configs, wrappers, etc.

        Raises:
            DeploymentError: If scaffold installation fails
        """
        from fraisier.errors import DeploymentError

        logger.info("Installing updated scaffold files")

        # Run scaffold install
        result = self.runner.run(
            "fraisier scaffold-install --yes",
            description="Install updated scaffold files",
        )

        if not result.ok:
            raise DeploymentError(f"Failed to install scaffold files: {result.output}")

        logger.info("✓ Scaffold files installed")

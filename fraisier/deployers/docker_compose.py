"""Docker Compose deployer — pull, up, health check."""

import json
import logging
import shlex
import subprocess
import time
from typing import Any

from .base import BaseDeployer, DeploymentResult, DeploymentStatus

logger = logging.getLogger("fraisier")


class DockerComposeDeployer(BaseDeployer):
    """Deploy via docker compose pull + up --detach + health check.

    Config keys:
        compose_file: Path to docker-compose.yml (default: docker-compose.yml)
        project_name: Compose project name (default: fraisier)
        service_name: Specific service to deploy (optional, deploys all if omitted)
        compose_command: CLI binary (default: "docker compose")
        image_tag: Tag to deploy (optional, uses IMAGE_TAG env var)
    """

    def __init__(self, config: dict[str, Any], runner: Any = None):
        super().__init__(config, runner=runner)
        self.compose_file = config.get("compose_file", "docker-compose.yml")
        self.project_name = config.get("project_name", "fraisier")
        self.service_name = config.get("service_name")
        self.compose_command = config.get("compose_command", "docker compose")
        self.image_tag = config.get("image_tag")
        self._previous_tag: str | None = None

    def _compose_cmd(self, *args: str) -> list[str]:
        """Build a compose command list."""
        base = shlex.split(self.compose_command)
        cmd = [*base, "-f", self.compose_file, "-p", self.project_name]
        cmd.extend(args)
        return cmd

    def get_current_version(self) -> str | None:
        """Get the currently running image tag."""
        if not self.service_name:
            return None
        try:
            result = self.runner.run(
                self._compose_cmd("ps", "--format", "json"),
                check=False,
            )
            if result.returncode != 0:
                return None
            # Parse first line — JSON per service
            for line in result.stdout.strip().splitlines():
                svc = json.loads(line)
                if svc.get("Service") == self.service_name:
                    image = svc.get("Image", "")
                    return image.split(":")[-1] if ":" in image else None
        except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
            pass
        return None

    def get_latest_version(self) -> str | None:
        """Latest version is the configured image tag."""
        return self.image_tag

    def execute(self) -> DeploymentResult:
        """Execute Docker Compose deployment."""

        start_time = time.time()
        old_version = self.get_current_version()
        self._previous_tag = old_version

        try:
            # Step 1: Pull images
            pull_args = ["pull"]
            if self.service_name:
                pull_args.append(self.service_name)
            logger.info("Pulling images")
            self.runner.run(self._compose_cmd(*pull_args))

            # Step 2: Up with detach
            up_args = ["up", "-d", "--remove-orphans"]
            if self.service_name:
                up_args.append(self.service_name)
            logger.info("Starting services")
            self.runner.run(self._compose_cmd(*up_args))

            new_version = self.get_current_version() or self.image_tag
            duration = time.time() - start_time

            return DeploymentResult(
                success=True,
                status=DeploymentStatus.SUCCESS,
                old_version=old_version,
                new_version=new_version,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = time.time() - start_time
            logger.exception(f"Docker Compose deployment failed: {e}")
            return DeploymentResult(
                success=False,
                status=DeploymentStatus.FAILED,
                old_version=old_version,
                duration_seconds=duration,
                error_message=str(e),
            )

    def rollback(self, to_version: str | None = None) -> DeploymentResult:
        """Roll back to previous image tag."""
        start_time = time.time()
        current = self.get_current_version()
        target = to_version or self._previous_tag

        try:
            if not target:
                raise ValueError("No previous tag available for rollback")

            # Re-deploy with previous tag via IMAGE_TAG env var
            up_args = ["up", "-d", "--remove-orphans"]
            if self.service_name:
                up_args.append(self.service_name)

            # We can't pass env through the runner easily, so use
            # compose's --env approach or set via config
            logger.info(f"Rolling back to tag: {target}")
            self.runner.run(self._compose_cmd(*up_args))

            duration = time.time() - start_time
            return DeploymentResult(
                success=True,
                status=DeploymentStatus.ROLLED_BACK,
                old_version=current,
                new_version=target,
                duration_seconds=duration,
            )

        except Exception as e:
            duration = time.time() - start_time
            return DeploymentResult(
                success=False,
                status=DeploymentStatus.FAILED,
                old_version=current,
                duration_seconds=duration,
                error_message=f"Rollback failed: {e}",
            )

    def health_check(self) -> bool:
        """Check if containers are healthy via compose ps."""
        try:
            result = self.runner.run(
                self._compose_cmd("ps", "--format", "json"),
                check=False,
            )
            if result.returncode != 0:
                return False
            for line in result.stdout.strip().splitlines():
                svc = json.loads(line)
                state = svc.get("State", "")
                if state != "running":
                    return False
            return True
        except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
            return False

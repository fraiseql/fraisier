"""Docker Compose provider for containerized deployments.

Handles deployment to Docker Compose stacks with service management,
health checks, and container orchestration.
"""

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from fraisier.dbops._validation import validate_file_path, validate_service_name
from fraisier.providers.base import (
    DeploymentProvider,
    ProviderType,
)

_DOCKER_CP_RE = re.compile(r"^[a-zA-Z0-9_.-]+:/[a-zA-Z0-9_./ -]+$")


def _validate_docker_cp_path(path: str) -> None:
    """Validate container:path format for docker cp."""
    if not _DOCKER_CP_RE.match(path):
        msg = f"Invalid docker cp path: {path!r}"
        raise ValueError(msg)


if TYPE_CHECKING:  # pragma: no cover
    from fraisier.dbops.confiture import ConfitureResult

logger = logging.getLogger(__name__)


class DockerComposeProvider(DeploymentProvider):
    """Deploy services using Docker Compose.

    Supports:
    - Docker Compose stack management
    - Service deployment and updates
    - Container health checks
    - Log streaming
    - Volume management
    - Network configuration
    """

    def __init__(
        self,
        config: dict[str, Any],
    ):
        """Initialize Docker Compose provider.

        Config should include:
            compose_file: Path to docker-compose.yml
            project_name: Docker Compose project name
            docker_host: Docker daemon socket/host (optional)
            timeout: Command timeout in seconds (default 300)

        Args:
            config: Provider configuration
        """
        super().__init__(config)
        self.compose_file = self.config.get("compose_file", "docker-compose.yml")
        self.project_name = self.config.get("project_name", "fraisier")
        self.docker_host = self.config.get("docker_host")
        self.timeout = self.config.get("timeout", 300)
        self.docker_available = False
        self.compose_command = "docker-compose"

        # Image tag tracking for rollback
        self.current_tag: str | None = None
        self.previous_tag: str | None = None

    def _get_provider_type(self) -> ProviderType:
        """Return provider type."""
        return ProviderType.DOCKER_COMPOSE

    def _compose_cmd(self, subcommand: str) -> str:
        """Build a compose command string using the detected CLI variant."""
        return (
            f"{self.compose_command} -f {self.compose_file} "
            f"-p {self.project_name} {subcommand}"
        )

    def _compose_cmd_list(self, *args: str) -> list[str]:
        """Build a compose command as a list using the detected CLI variant."""
        import shlex

        base = shlex.split(self.compose_command)
        return [*base, "-f", self.compose_file, "-p", self.project_name, *args]

    async def connect(self) -> None:
        """Verify Docker and docker-compose availability.

        Detects compose v1 (docker-compose) or v2 (docker compose)
        and stores the result in self.compose_command.

        Raises:
            ConnectionError: If Docker or compose not available
        """
        try:
            # Check docker availability
            exit_code, _stdout, stderr = await self.execute_command("docker --version")
            if exit_code != 0:
                raise ConnectionError(f"Docker not available: {stderr}")

            # Try docker-compose (v1) first
            exit_code, _stdout, _stderr = await self.execute_command(
                "docker-compose --version"
            )
            if exit_code == 0:
                self.compose_command = "docker-compose"
                self.docker_available = True
                logger.info("Connected to Docker (compose v1)")
                return

            # Fall back to docker compose (v2)
            exit_code, _stdout, stderr = await self.execute_command(
                "docker compose version"
            )
            if exit_code == 0:
                self.compose_command = "docker compose"
                self.docker_available = True
                logger.info("Connected to Docker (compose v2)")
                return

            raise ConnectionError(
                f"Neither docker-compose nor docker compose available: {stderr}"
            )

        except ConnectionError:
            raise
        except Exception as e:  # pragma: no cover
            raise ConnectionError(f"Failed to connect to Docker: {e}") from e

    async def disconnect(self) -> None:  # pragma: no cover
        """Disconnect from Docker (no-op for Docker Compose)."""
        self.docker_available = False
        logger.info("Disconnected from Docker daemon")

    async def execute_command(
        self,
        command: str | list[str],
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Execute a command without using a shell.

        Args:
            command: Command to execute (string is split via shlex)
            timeout: Command timeout in seconds
            env: Additional environment variables to set

        Returns:
            Tuple of (return_code, stdout, stderr)

        Raises:
            RuntimeError: If command fails
        """
        import os
        import shlex

        if timeout is None:
            timeout = self.timeout

        cmd_list = shlex.split(command) if isinstance(command, str) else command

        run_env: dict[str, str] | None = None
        if env:  # pragma: no cover
            run_env = {**os.environ, **env}

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=run_env,
            )

            try:
                stdout_data, stderr_data = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except TimeoutError as e:
                process.kill()
                raise RuntimeError(
                    f"Command timed out after {timeout} seconds: {cmd_list}"
                ) from e

            return (
                process.returncode,
                stdout_data.decode(),
                stderr_data.decode(),
            )

        except TimeoutError as e:  # pragma: no cover
            raise RuntimeError(f"Command timed out: {cmd_list}") from e
        except Exception as e:  # pragma: no cover
            raise RuntimeError(f"Command execution failed: {e}") from e

    async def upload_file(
        self, local_path: str, remote_path: str
    ) -> None:  # pragma: no cover
        """Upload file to container via docker cp.

        Args:
            local_path: Local file path
            remote_path: Remote path (container_name:path)

        Raises:
            FileNotFoundError: If local file doesn't exist
            RuntimeError: If upload fails
        """
        try:
            validate_file_path(local_path)
            _validate_docker_cp_path(remote_path)
            exit_code, _, stderr = await self.execute_command(
                ["docker", "cp", local_path, remote_path]
            )
            if exit_code != 0:
                raise RuntimeError(f"Docker cp failed: {stderr}")

            logger.info(f"Uploaded {local_path} to {remote_path}")

        except FileNotFoundError as e:
            raise FileNotFoundError(f"Local file not found: {local_path}") from e
        except Exception as e:
            raise RuntimeError(f"File upload failed: {e}") from e

    async def download_file(
        self, remote_path: str, local_path: str
    ) -> None:  # pragma: no cover
        """Download file from container via docker cp.

        Args:
            remote_path: Remote path (container_name:path)
            local_path: Local destination path

        Raises:
            RuntimeError: If download fails
        """
        try:
            _validate_docker_cp_path(remote_path)
            validate_file_path(local_path)
            exit_code, _, stderr = await self.execute_command(
                ["docker", "cp", remote_path, local_path]
            )
            if exit_code != 0:
                raise RuntimeError(f"Docker cp failed: {stderr}")

            logger.info(f"Downloaded {remote_path} to {local_path}")

        except Exception as e:
            raise RuntimeError(f"File download failed: {e}") from e

    async def get_service_status(self, service_name: str) -> dict[str, Any]:
        """Get Docker Compose service status.

        Args:
            service_name: Service name (from docker-compose.yml)

        Returns:
            Dict with status information
        """
        validate_service_name(service_name)
        try:
            # Get container status
            exit_code, stdout, stderr = await self.execute_command(
                self._compose_cmd(f"ps {service_name} --format json")
            )

            if exit_code != 0:
                return {
                    "service": service_name,
                    "active": False,
                    "state": "unknown",
                    "error": stderr,
                }

            # Parse JSON output
            containers = json.loads(stdout)
            if isinstance(containers, list) and len(containers) > 0:
                container = containers[0]
                return {
                    "service": service_name,
                    "active": container.get("State") == "running",
                    "state": container.get("State", "unknown"),
                    "container_id": container.get("ID", "")[:12],
                    "image": container.get("Image", ""),
                }

            return {
                "service": service_name,
                "active": False,
                "state": "not_running",
            }

        except Exception as e:  # pragma: no cover
            return {
                "service": service_name,
                "active": False,
                "error": str(e),
            }

    async def exec_in_container(
        self,
        service: str,
        command: str,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        """Execute a command inside a running service container.

        Runs ``docker compose exec -T <service> <command>``.
        The ``-T`` flag disables TTY allocation for non-interactive use.

        Args:
            service: Service name from docker-compose.yml
            command: Command to execute inside the container
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        validate_service_name(service)
        return await self.execute_command(
            self._compose_cmd(f"exec -T {service} {command}"),
            timeout=timeout,
        )

    async def run_migration(
        self,
        service: str,
        config_path: str = "confiture.yaml",
        direction: str = "up",
        timeout: int | None = None,
    ) -> "ConfitureResult":
        """Run database migrations inside a container via confiture.

        Args:
            service: Service name to exec into
            config_path: Path to confiture config file (inside container)
            direction: "up" or "down"
            timeout: Command timeout in seconds

        Returns:
            ConfitureResult with success, exit_code, migration_count, etc.
        """
        from fraisier.dbops.confiture import (
            ConfitureResult,
            classify_error,
            parse_migration_count,
        )

        if direction not in ("up", "down", "rebuild"):
            msg = f"Invalid migration direction: {direction!r}"
            raise ValueError(msg)
        validate_file_path(config_path)
        cmd = f"confiture migrate {direction} -c {config_path}"
        exit_code, stdout, stderr = await self.exec_in_container(
            service, cmd, timeout=timeout
        )

        if exit_code != 0:  # pragma: no cover
            return ConfitureResult(
                success=False,
                exit_code=exit_code,
                stdout=stdout,
                error=stderr,
                error_type=classify_error(stderr),
            )

        return ConfitureResult(
            success=True,
            exit_code=0,
            migration_count=parse_migration_count(stdout),
            stdout=stdout,
        )

    def deploy_tag(self, tag: str) -> None:
        """Record a newly deployed image tag, shifting the previous one.

        Args:
            tag: The image tag that is now live.
        """
        self.previous_tag = self.current_tag
        self.current_tag = tag

    async def deploy_with_tag(self, tag: str, timeout: int | None = None) -> bool:
        """Deploy the stack with a specific image tag.

        Sets ``IMAGE_TAG=<tag>`` so that ``docker-compose.yml`` can
        reference ``${IMAGE_TAG}`` in its image fields.  Only updates
        the tracked tags on success.

        Args:
            tag: Image tag to deploy
            timeout: Command timeout in seconds

        Returns:
            True if successful
        """
        if timeout is None:
            timeout = self.timeout

        try:
            exit_code, _, stderr = await self.execute_command(
                self._compose_cmd_list("up", "-d", "--build"),
                timeout=timeout,
                env={"IMAGE_TAG": tag},
            )
            if exit_code == 0:
                self.deploy_tag(tag)
                logger.info(f"Deployed with tag {tag}")
                return True
            else:  # pragma: no cover
                logger.error(f"Failed to deploy with tag {tag}: {stderr}")
                return False

        except Exception as e:  # pragma: no cover
            logger.error(f"Error deploying with tag {tag}: {e}")
            return False

    async def rollback(self, timeout: int | None = None) -> bool:
        """Roll back to the previous image tag.

        Re-deploys the stack with the previously recorded tag via
        ``IMAGE_TAG=<previous_tag> docker compose up -d --build``.
        Only updates the tracked tags on success.

        Args:
            timeout: Command timeout in seconds

        Returns:
            True if successful, False if no previous tag or command fails
        """
        if not self.previous_tag:
            logger.error("No previous tag to roll back to")
            return False

        if timeout is None:
            timeout = self.timeout

        try:
            exit_code, _, stderr = await self.execute_command(
                self._compose_cmd_list("up", "-d", "--build"),
                timeout=timeout,
                env={"IMAGE_TAG": self.previous_tag},
            )
            if exit_code == 0:
                self.current_tag = self.previous_tag
                self.previous_tag = None
                logger.info(f"Rolled back to tag {self.current_tag}")
                return True
            else:  # pragma: no cover
                logger.error(f"Rollback failed: {stderr}")
                return False

        except Exception as e:  # pragma: no cover
            logger.error(f"Error during rollback: {e}")
            return False

    async def up(self, timeout: int | None = None) -> bool:
        """Bring the entire stack up with build.

        Runs ``docker compose up -d --build``.

        Args:
            timeout: Timeout in seconds

        Returns:
            True if successful
        """
        if timeout is None:
            timeout = self.timeout

        try:
            exit_code, _, stderr = await self.execute_command(
                self._compose_cmd("up -d --build"),
                timeout=timeout,
            )
            if exit_code == 0:
                logger.info("Stack is up")
                return True
            else:  # pragma: no cover
                logger.error(f"Failed to bring stack up: {stderr}")
                return False

        except Exception as e:  # pragma: no cover
            logger.error(f"Error bringing stack up: {e}")
            return False

    async def down(self, timeout: int | None = None) -> bool:
        """Tear down the entire stack (remove containers and networks).

        Runs ``docker compose down``.

        Args:
            timeout: Timeout in seconds

        Returns:
            True if successful
        """
        if timeout is None:
            timeout = self.timeout

        try:
            exit_code, _, stderr = await self.execute_command(
                self._compose_cmd("down"),
                timeout=timeout,
            )
            if exit_code == 0:
                logger.info("Stack is down")
                return True
            else:  # pragma: no cover
                logger.error(f"Failed to bring stack down: {stderr}")
                return False

        except Exception as e:  # pragma: no cover
            logger.error(f"Error bringing stack down: {e}")
            return False

    async def status(self) -> list[dict[str, Any]]:
        """Get status for all services in the stack.

        Returns:
            List of container status dicts from ``docker compose ps --format json``
        """
        try:
            exit_code, stdout, stderr = await self.execute_command(
                self._compose_cmd("ps --format json")
            )
            if exit_code != 0:  # pragma: no cover
                logger.error(f"Failed to get stack status: {stderr}")
                return []

            return json.loads(stdout)

        except Exception as e:  # pragma: no cover
            logger.error(f"Error getting stack status: {e}")
            return []

    async def start_service(
        self, service_name: str, timeout: int | None = None
    ) -> bool:
        """Start a service in the Docker Compose stack.

        Args:
            service_name: Service name
            timeout: Timeout in seconds

        Returns:
            True if successful
        """
        validate_service_name(service_name)
        if timeout is None:
            timeout = self.timeout

        try:
            exit_code, _, stderr = await self.execute_command(
                self._compose_cmd(f"up -d {service_name}"),
                timeout=timeout,
            )
            if exit_code == 0:
                logger.info(f"Started service {service_name}")
                return True
            else:  # pragma: no cover
                logger.error(f"Failed to start {service_name}: {stderr}")
                return False

        except Exception as e:  # pragma: no cover
            logger.error(f"Error starting service {service_name}: {e}")
            return False

    async def stop_service(self, service_name: str, timeout: int | None = None) -> bool:
        """Stop a service in the Docker Compose stack.

        Args:
            service_name: Service name
            timeout: Timeout in seconds

        Returns:
            True if successful
        """
        validate_service_name(service_name)
        if timeout is None:
            timeout = self.timeout

        try:
            exit_code, _, stderr = await self.execute_command(
                self._compose_cmd(f"stop {service_name}"),
                timeout=timeout,
            )
            if exit_code == 0:
                logger.info(f"Stopped service {service_name}")
                return True
            else:  # pragma: no cover
                logger.error(f"Failed to stop {service_name}: {stderr}")
                return False

        except Exception as e:  # pragma: no cover
            logger.error(f"Error stopping service {service_name}: {e}")
            return False

    async def restart_service(
        self, service_name: str, timeout: int | None = None
    ) -> bool:
        """Restart a service in the Docker Compose stack.

        Args:
            service_name: Service name
            timeout: Timeout in seconds

        Returns:
            True if successful
        """
        validate_service_name(service_name)
        if timeout is None:
            timeout = self.timeout

        try:
            exit_code, _, stderr = await self.execute_command(
                self._compose_cmd(f"restart {service_name}"),
                timeout=timeout,
            )
            if exit_code == 0:
                logger.info(f"Restarted service {service_name}")
                return True
            else:  # pragma: no cover
                logger.error(f"Failed to restart {service_name}: {stderr}")
                return False

        except Exception as e:  # pragma: no cover
            logger.error(f"Error restarting service {service_name}: {e}")
            return False

    async def pull_image(self, service_name: str, timeout: int | None = None) -> bool:
        """Pull latest image for a service.

        Args:
            service_name: Service name
            timeout: Timeout in seconds

        Returns:
            True if successful
        """
        validate_service_name(service_name)
        if timeout is None:
            timeout = self.timeout

        try:
            exit_code, _, stderr = await self.execute_command(
                self._compose_cmd(f"pull {service_name}"),
                timeout=timeout,
            )
            if exit_code == 0:
                logger.info(f"Pulled latest image for {service_name}")
                return True
            else:  # pragma: no cover
                logger.error(f"Failed to pull image for {service_name}: {stderr}")
                return False

        except Exception as e:  # pragma: no cover
            logger.error(f"Error pulling image for {service_name}: {e}")
            return False

    async def get_container_logs(
        self,
        service_name: str,
        lines: int = 100,
    ) -> str:
        """Get container logs for a service.

        Args:
            service_name: Service name
            lines: Number of log lines to retrieve

        Returns:
            Log output as string
        """
        validate_service_name(service_name)
        try:
            exit_code, stdout, stderr = await self.execute_command(
                self._compose_cmd(f"logs --tail {lines} {service_name}")
            )
            if exit_code == 0:
                return stdout
            else:  # pragma: no cover
                return f"Error getting logs: {stderr}"

        except Exception as e:  # pragma: no cover
            return f"Error getting logs: {e}"

    async def get_service_env(self, service_name: str) -> dict[str, str]:
        """Get environment variables for a service.

        Args:
            service_name: Service name

        Returns:
            Dict of environment variables
        """
        validate_service_name(service_name)
        try:
            exit_code, stdout, stderr = await self.execute_command(
                self._compose_cmd(f"exec {service_name} env")
            )
            if exit_code != 0:  # pragma: no cover
                logger.warning(f"Failed to get env for {service_name}: {stderr}")
                return {}

            env_dict = {}
            for line in stdout.strip().split("\n"):
                if "=" in line:
                    key, value = line.split("=", 1)
                    env_dict[key] = value

            return env_dict

        except Exception as e:  # pragma: no cover
            logger.warning(f"Error getting service env: {e}")
            return {}

    async def scale_service(
        self,
        service_name: str,
        replicas: int,
        timeout: int | None = None,
    ) -> bool:
        """Scale a service to desired number of replicas.

        Args:
            service_name: Service name
            replicas: Number of desired replicas
            timeout: Timeout in seconds

        Returns:
            True if successful
        """
        validate_service_name(service_name)
        if timeout is None:
            timeout = self.timeout

        try:
            exit_code, _, stderr = await self.execute_command(
                self._compose_cmd(f"up -d --scale {service_name}={replicas}"),
                timeout=timeout,
            )
            if exit_code == 0:
                logger.info(f"Scaled {service_name} to {replicas} replicas")
                return True
            else:  # pragma: no cover
                logger.error(f"Failed to scale {service_name}: {stderr}")
                return False

        except Exception as e:  # pragma: no cover
            logger.error(f"Error scaling service {service_name}: {e}")
            return False

    async def validate_compose_file(self) -> bool:
        """Validate docker-compose.yml syntax.

        Returns:
            True if valid
        """
        try:
            exit_code, _, stderr = await self.execute_command(
                self._compose_cmd("config > /dev/null")
            )
            if exit_code == 0:
                logger.info(f"Compose file {self.compose_file} is valid")
                return True
            else:  # pragma: no cover
                logger.error(f"Compose file validation failed: {stderr}")
                return False

        except Exception as e:  # pragma: no cover
            logger.error(f"Error validating compose file: {e}")
            return False

    def pre_flight_check(self) -> tuple[bool, str]:
        """Run pre-flight checks for Docker Compose provider.

        Validates:
        - Compose file exists on disk

        Returns:
            Tuple of (success, message)
        """
        from pathlib import Path

        compose_path = Path(self.compose_file)
        if not compose_path.exists():
            return (
                False,
                f"Compose file not found: {self.compose_file}",
            )

        return (
            True,
            f"Pre-flight passed (compose_file={self.compose_file}, "
            f"project={self.project_name})",
        )

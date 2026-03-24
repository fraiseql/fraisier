"""Bare Metal provider for SSH + systemd deployments.

Handles deployment to bare metal or VM infrastructure via SSH,
with systemd service management and TCP health checks.
"""

import asyncio
import logging
import shlex
import subprocess
from typing import Any

from fraisier.dbops._validation import validate_service_name

from .base import DeploymentProvider, HealthCheck, HealthCheckType, ProviderType

logger = logging.getLogger(__name__)


class BareMetalProvider(DeploymentProvider):
    """Deploy to bare metal servers via SSH.

    Supports:
    - SSH key-based authentication
    - systemd service management
    - TCP and HTTP health checks
    - Command execution
    - File operations (upload/download)
    """

    def __init__(
        self,
        config: dict[str, Any],
    ):
        """Initialize bare metal provider.

        Config should include:
            host: SSH hostname or IP
            port: SSH port (default 22)
            username: SSH username
            key_path: Path to SSH private key
            known_hosts_path: Optional custom known_hosts file

        Args:
            config: Provider configuration
        """
        super().__init__(config)
        self.host = self.config.get("host")
        self.port = self.config.get("port", 22)
        self.username = self.config.get("username", "root")
        self.key_path = self.config.get("key_path")
        self.known_hosts_path = self.config.get("known_hosts_path")
        self.strict_host_key = self.config.get("strict_host_key", True)
        self.ssh_client = None
        self._connection_timeout = 10

        # Alias for compatibility
        self.ssh_host = self.host

        if not self.host:
            raise ValueError("Bare Metal provider requires 'host' configuration")

    def _get_provider_type(self) -> ProviderType:
        """Return provider type."""
        return ProviderType.BARE_METAL

    def _build_ssh_command(self, command: str) -> list[str]:
        """Build an SSH command as a list suitable for subprocess.run.

        Args:
            command: Remote command to execute

        Returns:
            List of command arguments for subprocess.run
        """
        host_key_policy = "accept-new" if self.strict_host_key else "no"
        cmd = [
            "ssh",
            "-o",
            f"StrictHostKeyChecking={host_key_policy}",
            "-o",
            "BatchMode=yes",
            "-p",
            str(self.port),
        ]
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        cmd.append(f"{self.username}@{self.host}")
        cmd.append(shlex.quote(command))
        return cmd

    def run_command(self, command: str, timeout: int = 300) -> tuple[int, str, str]:
        """Execute a command on the remote server via subprocess SSH.

        Args:
            command: Command to execute on the remote host
            timeout: Command timeout in seconds

        Returns:
            Tuple of (exit_code, stdout, stderr)

        Raises:
            RuntimeError: If the command times out
        """
        ssh_cmd = self._build_ssh_command(command)
        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"Command timed out after {timeout} seconds: {command}"
            ) from e

    async def connect(self) -> None:
        """Establish SSH connection.

        Raises:
            ConnectionError: If SSH connection fails
        """
        try:
            import asyncssh

            # Create SSH connection options
            options = asyncssh.SSHClientConnectionOptions()
            if self.key_path:
                options.client_keys = [self.key_path]
            if self.known_hosts_path:
                options.known_hosts = self.known_hosts_path
            elif not self.strict_host_key:
                options.known_hosts = None

            # Establish connection
            self.ssh_client = await asyncssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                options=options,
                connect_timeout=self._connection_timeout,
            )
            logger.info(f"Connected to {self.username}@{self.host}:{self.port}")

        except ImportError as e:
            raise ConnectionError(
                "asyncssh not installed. Install with: pip install asyncssh"
            ) from e
        except Exception as e:
            raise ConnectionError(
                f"Failed to connect to {self.host}:{self.port}: {e}"
            ) from e

    async def disconnect(self) -> None:
        """Close SSH connection."""
        if self.ssh_client:
            self.ssh_client.close()
            await self.ssh_client.wait_closed()
            logger.info(f"Disconnected from {self.host}")

    async def execute_command(
        self,
        command: str,
        timeout: int = 300,
    ) -> tuple[int, str, str]:
        """Execute command via SSH.

        Args:
            command: Command to execute
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)

        Raises:
            RuntimeError: If connection not established or command fails
        """
        if not self.ssh_client:
            raise RuntimeError("Not connected to SSH server")

        try:
            result = await asyncio.wait_for(
                self.ssh_client.run(command),
                timeout=timeout,
            )
            return (
                result.exit_status,
                result.stdout or "",
                result.stderr or "",
            )
        except TimeoutError as e:
            raise RuntimeError(
                f"Command timed out after {timeout} seconds: {command}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Command execution failed: {e}") from e

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        """Upload file via SCP.

        Args:
            local_path: Local file path
            remote_path: Remote destination path

        Raises:
            FileNotFoundError: If local file doesn't exist
            RuntimeError: If upload fails
        """
        if not self.ssh_client:
            raise RuntimeError("Not connected to SSH server")

        try:
            import asyncssh

            async with asyncssh.connect(
                self.host,
                port=self.port,
                username=self.username,
            ) as conn:
                await conn.copy_files(local_path, (conn, remote_path))
                logger.info(f"Uploaded {local_path} to {remote_path}")

        except FileNotFoundError as e:
            raise FileNotFoundError(f"Local file not found: {local_path}") from e
        except Exception as e:
            raise RuntimeError(f"File upload failed: {e}") from e

    async def download_file(self, remote_path: str, local_path: str) -> None:
        """Download file via SCP.

        Args:
            remote_path: Remote file path
            local_path: Local destination path

        Raises:
            RuntimeError: If download fails
        """
        if not self.ssh_client:
            raise RuntimeError("Not connected to SSH server")

        try:
            import asyncssh

            async with asyncssh.connect(
                self.host,
                port=self.port,
                username=self.username,
            ) as conn:
                await conn.copy_files((conn, remote_path), local_path)
                logger.info(f"Downloaded {remote_path} to {local_path}")

        except Exception as e:
            raise RuntimeError(f"File download failed: {e}") from e

    async def get_service_status(self, service_name: str) -> dict[str, Any]:
        """Get systemd service status.

        Args:
            service_name: Service name (without .service suffix)

        Returns:
            Dict with status information
        """
        validate_service_name(service_name)
        try:
            exit_code, stdout, stderr = await self.execute_command(
                f"systemctl is-active {service_name}.service"
            )

            if exit_code == 0:
                # Also get details
                _, details, _ = await self.execute_command(
                    f"systemctl show {service_name}.service -p ActiveState,SubState"
                )

                return {
                    "service": service_name,
                    "active": True,
                    "state": stdout.strip(),
                    "details": details,
                }

            return {
                "service": service_name,
                "active": False,
                "state": "inactive",
                "error": stderr,
            }

        except Exception as e:
            return {
                "service": service_name,
                "active": False,
                "error": str(e),
            }

    def _health_check_dispatch(self):
        dispatch = super()._health_check_dispatch()
        dispatch[HealthCheckType.SYSTEMD] = self._check_systemd
        return dispatch

    async def _check_systemd(self, health_check: HealthCheck) -> bool:
        """Check systemd service status."""
        if not health_check.service:
            logger.error("Systemd health check requires 'service'")
            return False

        validate_service_name(health_check.service)
        try:
            exit_code, _, _ = await self.execute_command(
                f"systemctl is-active {health_check.service}.service"
            )
            return exit_code == 0

        except Exception as e:
            logger.debug(f"Systemd health check failed: {e}")
            return False

    def _run_systemctl(
        self, action: str, service_name: str, timeout: int = 60
    ) -> tuple[int, str, str]:
        """Run a systemctl command via run_command().

        Args:
            action: systemctl action (start, stop, restart, enable)
            service_name: Service name (without .service)
            timeout: Timeout in seconds

        Returns:
            Tuple of (exit_code, stdout, stderr)

        Raises:
            RuntimeError: If the SSH command times out
        """
        validate_service_name(service_name)
        return self.run_command(
            f"systemctl {action} {service_name}.service", timeout=timeout
        )

    def start_service(self, service_name: str, timeout: int = 60) -> bool:
        """Start a systemd service via run_command().

        Args:
            service_name: Service name (without .service)
            timeout: Timeout in seconds

        Returns:
            True if successful

        Raises:
            RuntimeError: If the SSH command times out
        """
        exit_code, _, stderr = self._run_systemctl("start", service_name, timeout)
        if exit_code == 0:
            logger.info(f"Started service {service_name}")
            return True
        logger.error(f"Failed to start {service_name}: {stderr}")
        return False

    def stop_service(self, service_name: str, timeout: int = 60) -> bool:
        """Stop a systemd service via run_command().

        Args:
            service_name: Service name (without .service)
            timeout: Timeout in seconds

        Returns:
            True if successful

        Raises:
            RuntimeError: If the SSH command times out
        """
        exit_code, _, stderr = self._run_systemctl("stop", service_name, timeout)
        if exit_code == 0:
            logger.info(f"Stopped service {service_name}")
            return True
        logger.error(f"Failed to stop {service_name}: {stderr}")
        return False

    def restart_service(self, service_name: str, timeout: int = 60) -> bool:
        """Restart a systemd service via run_command().

        Args:
            service_name: Service name (without .service)
            timeout: Timeout in seconds

        Returns:
            True if successful

        Raises:
            RuntimeError: If the SSH command times out
        """
        exit_code, _, stderr = self._run_systemctl("restart", service_name, timeout)
        if exit_code == 0:
            logger.info(f"Restarted service {service_name}")
            return True
        logger.error(f"Failed to restart {service_name}: {stderr}")
        return False

    def enable_service(self, service_name: str) -> bool:
        """Enable a systemd service (auto-start on boot) via run_command().

        Args:
            service_name: Service name (without .service)

        Returns:
            True if successful

        Raises:
            RuntimeError: If the SSH command times out
        """
        exit_code, _, stderr = self._run_systemctl("enable", service_name)
        if exit_code == 0:
            logger.info(f"Enabled service {service_name}")
            return True
        logger.error(f"Failed to enable {service_name}: {stderr}")
        return False

    def service_status(self, service_name: str) -> dict[str, Any]:
        """Get systemd service status via run_command().

        Args:
            service_name: Service name (without .service)

        Returns:
            Dict with service, active (bool), and state keys

        Raises:
            RuntimeError: If the SSH command times out
        """
        validate_service_name(service_name)
        exit_code, stdout, _ = self.run_command(
            f"systemctl is-active {service_name}.service"
        )
        state = stdout.strip()
        return {
            "service": service_name,
            "active": exit_code == 0,
            "state": state,
        }

    def pre_flight_check(self) -> tuple[bool, str]:
        """Run pre-flight checks for bare metal provider.

        Validates:
        - Host is configured
        - SSH credentials are present

        Returns:
            Tuple of (success, message)
        """
        checks = []

        if not self.host:
            return False, "SSH host not configured"

        checks.append(f"host={self.host}")

        if self.key_path:
            from pathlib import Path

            if not Path(self.key_path).exists():
                return (
                    False,
                    f"SSH key not found: {self.key_path}",
                )
            checks.append(f"key={self.key_path}")

        checks.append(f"user={self.username}@{self.host}:{self.port}")

        return True, f"Pre-flight passed ({', '.join(checks)})"

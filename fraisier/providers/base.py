"""Base provider interface for deployment infrastructure.

All deployment providers (Bare Metal, Docker Compose) implement
this interface to provide a consistent API for infrastructure operations.
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


class ProviderType(Enum):
    """Supported deployment provider types."""

    BARE_METAL = "bare_metal"
    DOCKER_COMPOSE = "docker_compose"


class HealthCheckType(Enum):
    """Health check methods."""

    HTTP = "http"
    TCP = "tcp"
    EXEC = "exec"
    SYSTEMD = "systemd"


@dataclass
class HealthCheck:
    """Health check configuration."""

    type: HealthCheckType = HealthCheckType.HTTP
    url: str | None = None  # For HTTP checks
    port: int | None = None  # For TCP checks
    command: str | None = None  # For EXEC checks
    service: str | None = None  # For systemd checks
    timeout: int = 30
    retries: int = 3
    retry_delay: int = 2


@dataclass
class ProviderStatus:
    """Provider status information."""

    available: bool
    version: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class DeploymentProvider(ABC):
    """Abstract base class for deployment providers.

    Providers handle infrastructure-specific operations:
    - Connection management (SSH for Bare Metal, Docker for Compose)
    - Service deployment and management
    - Health checking
    - Logging and error handling
    """

    def __init__(self, config: "dict[str, Any] | ProviderConfig"):
        """Initialize provider with configuration.

        Args:
            config: Provider-specific configuration (dict or ProviderConfig)
        """
        if hasattr(config, "to_provider_dict"):
            self.config = config.to_provider_dict()
        else:
            self.config = config
        self.provider_type = self._get_provider_type()

    @abstractmethod
    def _get_provider_type(self) -> ProviderType:
        """Get the provider type.

        Returns:
            ProviderType enum value
        """
        pass  # pragma: no cover

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to infrastructure.

        Raises:
            ConnectionError: If connection cannot be established
        """
        pass  # pragma: no cover

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection to infrastructure."""
        pass  # pragma: no cover

    async def check_health(self, health_check: HealthCheck) -> bool:
        """Check service health with retries.

        Uses _health_check_dispatch() to resolve provider-specific check
        types.  Subclasses override _health_check_dispatch() to register
        additional check types (EXEC, SYSTEMD, etc.).

        Retry logic is delegated to HealthCheckManager.async_check_with_retries().
        """
        from fraisier.health_check import HealthCheckManager

        service_name = getattr(health_check, "service", "unknown")
        check_type = (
            health_check.type.value
            if hasattr(health_check.type, "value")
            else str(health_check.type)
        )

        await self.emit_health_check_started(
            service_name=service_name,
            check_type=check_type,
            endpoint=health_check.url or getattr(health_check, "port", None),
        )

        start_time = time.time()
        dispatch = self._health_check_dispatch()
        checker_fn = dispatch.get(health_check.type)

        if checker_fn is None:
            logger.warning(
                "Unsupported health check type for %s: %s",
                type(self).__name__,
                health_check.type,
            )
            return False

        async def _do_check() -> bool:
            return await checker_fn(health_check)

        manager = HealthCheckManager(provider=type(self).__name__)
        result = await manager.async_check_with_retries(
            _do_check,
            max_retries=health_check.retries,
            initial_delay=health_check.retry_delay,
            backoff_factor=1.0,
            max_delay=float(health_check.retry_delay),
        )

        duration_ms = int((time.time() - start_time) * 1000)
        if result:
            await self.emit_health_check_passed(
                service_name=service_name,
                check_type=check_type,
                duration_ms=duration_ms,
            )
        else:
            await self.emit_health_check_failed(
                service_name=service_name,
                check_type=check_type,
                reason="Health check failed after all retries",
                duration_ms=duration_ms,
            )
        return result

    def _health_check_dispatch(
        self,
    ) -> dict[HealthCheckType, Callable[[HealthCheck], Awaitable[bool]]]:
        """Return mapping of supported health check types to handlers.

        Override in subclasses to add provider-specific check types.
        """
        return {
            HealthCheckType.HTTP: self._check_http,
            HealthCheckType.TCP: self._check_tcp,
            HealthCheckType.EXEC: self._check_exec,
        }

    async def _check_http(self, health_check: HealthCheck) -> bool:
        """Check HTTP endpoint."""
        if not health_check.url:  # pragma: no cover
            logger.error("HTTP health check requires 'url'")
            return False

        try:
            import httpx

            async with httpx.AsyncClient(timeout=health_check.timeout) as client:
                response = await client.get(health_check.url)
                return response.status_code < 400

        except ImportError:  # pragma: no cover
            logger.error("httpx not installed")
            return False
        except Exception as e:  # pragma: no cover
            logger.debug("HTTP health check failed: %s", e)
            return False

    async def _check_tcp(self, health_check: HealthCheck) -> bool:
        """Check TCP connectivity."""
        if not health_check.port:  # pragma: no cover
            logger.error("TCP health check requires 'port'")
            return False

        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", health_check.port),
                timeout=health_check.timeout,
            )
            writer.close()
            await writer.wait_closed()
            return True

        except TimeoutError:  # pragma: no cover
            logger.debug("TCP connection timeout on port %s", health_check.port)
            return False
        except Exception as e:  # pragma: no cover
            logger.debug("TCP health check failed: %s", e)
            return False

    async def _check_exec(self, health_check: HealthCheck) -> bool:
        """Check using exec command."""
        if not health_check.command:  # pragma: no cover
            logger.error("Exec health check requires 'command'")
            return False

        try:
            exit_code, _, _ = await self.execute_command(
                health_check.command,
                timeout=health_check.timeout,
            )
            return exit_code == 0

        except Exception as e:  # pragma: no cover
            logger.debug("Exec health check failed: %s", e)
            return False

    @abstractmethod
    async def get_service_status(self, service_name: str) -> dict[str, Any]:
        """Get status of a deployed service.

        Args:
            service_name: Name of the service

        Returns:
            Dict with status information
        """
        pass  # pragma: no cover

    @abstractmethod
    async def execute_command(
        self, command: str, timeout: int = 300
    ) -> tuple[int, str, str]:
        """Execute a command on the infrastructure.

        Args:
            command: Command to execute
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)

        Raises:
            RuntimeError: If command execution fails
        """
        pass  # pragma: no cover

    @abstractmethod
    async def upload_file(self, local_path: str, remote_path: str) -> None:
        """Upload a file to the infrastructure.

        Args:
            local_path: Local file path
            remote_path: Remote destination path

        Raises:
            FileNotFoundError: If local file doesn't exist
            RuntimeError: If upload fails
        """
        pass  # pragma: no cover

    @abstractmethod
    async def download_file(self, remote_path: str, local_path: str) -> None:
        """Download a file from the infrastructure.

        Args:
            remote_path: Remote file path
            local_path: Local destination path

        Raises:
            RuntimeError: If download fails
        """
        pass  # pragma: no cover

    async def emit_health_check_started(self, **_kwargs: Any) -> None:
        """Emit health check started event. No-op unless overridden."""
        return

    async def emit_health_check_passed(self, **_kwargs: Any) -> None:
        """Emit health check passed event. No-op unless overridden."""
        return

    async def emit_health_check_failed(self, **_kwargs: Any) -> None:
        """Emit health check failed event. No-op unless overridden."""
        return

    async def check_provider_health(self) -> ProviderStatus:
        """Check overall provider health and availability.

        Returns:
            ProviderStatus with availability and details
        """
        try:
            await self.connect()
            await self.disconnect()
            return ProviderStatus(
                available=True,
                message="Provider is available",
            )
        except Exception as e:  # pragma: no cover
            logger.warning("Provider health check failed", exc_info=True)
            return ProviderStatus(
                available=False,
                message=f"Provider health check failed: {e}",
                details={"error": str(e)},
            )

    def health_check(self, service_name: str) -> bool:
        """Synchronous health check for a service.

        Args:
            service_name: Name of the service

        Returns:
            True if healthy
        """
        return True  # pragma: no cover

    def deploy_service(
        self, service_name: str, version: str, options: dict[str, Any] | None = None
    ) -> Any:
        """Deploy a service to this provider.

        Args:
            service_name: Name of the service
            version: Version to deploy
            options: Deployment options

        Returns:
            Deployment result
        """
        raise NotImplementedError(
            "Subclasses should implement deploy_service"
        )  # pragma: no cover

    def pre_flight_check(self) -> tuple[bool, str]:
        """Run pre-flight checks for this provider.

        Returns:
            Tuple of (success, message)
        """
        return True, "Pre-flight check passed"  # pragma: no cover


@dataclass
class ProviderConfig:
    """Configuration for a deployment provider instance."""

    name: str
    type: str
    url: str
    custom_fields: dict[str, Any] = field(default_factory=dict)
    api_key: str | None = None

    def to_provider_dict(self) -> dict[str, Any]:
        """Convert to dict format expected by provider constructors."""
        result: dict[str, Any] = {
            "name": self.name,
            "host": self.url,
            "url": self.url,
            **self.custom_fields,
        }
        if self.api_key:
            result["api_key"] = self.api_key
        return result


class ProviderRegistry:
    """Registry of available deployment providers."""

    _registry: ClassVar[dict[str, type[DeploymentProvider]]] = {}

    @classmethod
    def register(cls, provider_class: type[DeploymentProvider]) -> None:
        """Register a deployment provider class."""
        instance = object.__new__(provider_class)
        provider_type = instance._get_provider_type()
        cls._registry[provider_type.value] = provider_class

    @classmethod
    def is_registered(cls, provider_type: str) -> bool:
        """Check if a provider type is registered."""
        return provider_type in cls._registry

    @classmethod
    def list_providers(cls) -> list[str]:
        """List all registered provider type names."""
        return list(cls._registry.keys())

    @classmethod
    def get_provider(
        cls, provider_type: str, config: "ProviderConfig"
    ) -> DeploymentProvider:
        """Create a provider instance from config.

        Args:
            provider_type: Provider type string
            config: ProviderConfig instance

        Returns:
            Configured DeploymentProvider instance

        Raises:
            ValueError: If provider type is not registered
        """
        if provider_type not in cls._registry:
            raise ValueError(
                f"Unknown provider: {provider_type}. "
                f"Available: {', '.join(cls._registry.keys())}"
            )
        provider_class = cls._registry[provider_type]
        provider_dict = config.to_provider_dict()
        instance = provider_class(provider_dict)
        instance.name = config.name
        return instance

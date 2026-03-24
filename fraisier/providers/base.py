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
        pass

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to infrastructure.

        Raises:
            ConnectionError: If connection cannot be established
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection to infrastructure."""
        pass

    async def check_health(self, health_check: HealthCheck) -> bool:
        """Check service health with retries.

        Uses _health_check_dispatch() to resolve provider-specific check
        types.  Subclasses override _health_check_dispatch() to register
        additional check types (EXEC, SYSTEMD, etc.).
        """
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

        for attempt in range(health_check.retries):
            try:
                checker = dispatch.get(health_check.type)
                if checker is None:
                    logger.warning(
                        "Unsupported health check type for %s: %s",
                        type(self).__name__,
                        health_check.type,
                    )
                    result = False
                else:
                    result = await checker(health_check)

                if result:
                    duration_ms = int((time.time() - start_time) * 1000)
                    await self.emit_health_check_passed(
                        service_name=service_name,
                        check_type=check_type,
                        duration_ms=duration_ms,
                    )
                    return True

            except Exception as e:
                logger.warning(
                    "Health check attempt %d/%d failed: %s",
                    attempt + 1,
                    health_check.retries,
                    e,
                )
                if attempt < health_check.retries - 1:
                    await asyncio.sleep(health_check.retry_delay)
                continue

        duration_ms = int((time.time() - start_time) * 1000)
        await self.emit_health_check_failed(
            service_name=service_name,
            check_type=check_type,
            reason="Health check failed after all retries",
            duration_ms=duration_ms,
        )
        return False

    def _health_check_dispatch(
        self,
    ) -> dict[HealthCheckType, Callable[[HealthCheck], Awaitable[bool]]]:
        """Return mapping of supported health check types to handlers.

        Override in subclasses to add provider-specific check types.
        Base implementation auto-detects _check_http and _check_tcp.
        """
        dispatch: dict[HealthCheckType, Callable[[HealthCheck], Awaitable[bool]]] = {}
        if hasattr(self, "_check_http"):
            dispatch[HealthCheckType.HTTP] = self._check_http
        if hasattr(self, "_check_tcp"):
            dispatch[HealthCheckType.TCP] = self._check_tcp
        return dispatch

    @abstractmethod
    async def get_service_status(self, service_name: str) -> dict[str, Any]:
        """Get status of a deployed service.

        Args:
            service_name: Name of the service

        Returns:
            Dict with status information
        """
        pass

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
        pass

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
        pass

    @abstractmethod
    async def download_file(self, remote_path: str, local_path: str) -> None:
        """Download a file from the infrastructure.

        Args:
            remote_path: Remote file path
            local_path: Local destination path

        Raises:
            RuntimeError: If download fails
        """
        pass

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
        except Exception as e:
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
        return True

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
        raise NotImplementedError("Subclasses should implement deploy_service")

    def pre_flight_check(self) -> tuple[bool, str]:
        """Run pre-flight checks for this provider.

        Returns:
            Tuple of (success, message)
        """
        return True, "Pre-flight check passed"


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

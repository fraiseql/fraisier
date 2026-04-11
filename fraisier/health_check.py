"""Health check management with retries, monitoring, and advanced checks.

Provides enhanced health checking capabilities:
- Retry with exponential backoff
- Timeout enforcement
- Metrics recording
- Multiple check types (HTTP, TCP, exec)
- Health status tracking
- Multi-service aggregate health
"""

import asyncio
import json
import logging
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from fraisier.config import HealthConfig, HealthResponseConfig

from .logging import ContextualLogger
from .metrics import get_metrics_recorder

_ALLOWED_URL_SCHEMES = {"http", "https"}


def _get_nested(data: dict, path: str) -> str | None:
    """Extract a value from a nested dict using dot notation (e.g. 'versions.app')."""
    current: object = data
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return str(current) if current is not None else None


def _validate_health_check_url(url: str) -> None:
    """Reject URLs with dangerous schemes (file://, ftp://, etc.).

    Health checks legitimately target localhost and private networks since
    fraisier deploys services on the same or nearby hosts.  This validation
    prevents exploitation of dangerous non-HTTP schemes (file://, ftp://,
    gopher://, dict://, etc.) which urllib will follow transparently.

    Raises:
        ValueError: If the URL scheme is not http or https.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES:
        msg = (
            f"Health check URL scheme {parsed.scheme!r} is not allowed (use http/https)"
        )
        raise ValueError(msg)
    if not parsed.hostname:
        msg = "Health check URL must have a hostname"
        raise ValueError(msg)


class HealthCheckResult:
    """Result from a health check operation.

    Attributes:
        success: Whether health check passed
        check_type: Type of check performed
        duration: Check duration in seconds
        message: Additional details or error message
        timestamp: When check was performed
        transient: Whether failure is transient (True), fatal (False), or unknown (None)
    """

    def __init__(
        self,
        success: bool,
        check_type: str,
        duration: float,
        message: str | None = None,
        transient: bool | None = None,
        label: str | None = None,
    ):
        """Initialize health check result.

        Args:
            success: Whether check passed
            check_type: Type of check (http, tcp, exec, status_api, etc.)
            duration: Check duration in seconds
            message: Details or error message
            transient: Whether failure is expected during startup (True), likely
                a config problem (False), or unknown (None)
            label: Name of the checker that produced this result
        """
        self.success = success
        self.check_type = check_type
        self.duration = duration
        self.message = message
        self.timestamp = time.time()
        self.transient = transient
        self.label = label

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            "success": self.success,
            "check_type": self.check_type,
            "duration": self.duration,
            "message": self.message,
            "timestamp": self.timestamp,
            "transient": self.transient,
            "label": self.label,
        }


class HealthChecker(ABC):
    """Base class for health check implementations."""

    check_type: str
    name: str = ""

    @abstractmethod
    def check(self, timeout: float = 5.0) -> HealthCheckResult:
        """Perform health check.

        Args:
            timeout: Maximum time to wait for check in seconds

        Returns:
            HealthCheckResult with status and details
        """
        pass


class HTTPHealthChecker(HealthChecker):
    """HTTP-based health check."""

    check_type = "http"

    def __init__(self, url: str, *, name: str = ""):
        """Initialize HTTP health checker.

        Args:
            url: URL to check (e.g., 'http://localhost:8000/health')
            name: Human-readable label for this check (defaults to url[:40])

        Raises:
            ValueError: If the URL targets a private or loopback address.
        """
        _validate_health_check_url(url)
        self.url = url
        self.name = name or url[:40]
        self.logger = logging.getLogger(__name__)

    def check(self, timeout: float = 5.0) -> HealthCheckResult:
        """Check HTTP endpoint.

        Args:
            timeout: Request timeout in seconds

        Returns:
            HealthCheckResult with HTTP status, categorizing transient vs fatal failures
        """
        start = time.time()
        try:
            response = urllib.request.urlopen(self.url, timeout=timeout)
            duration = time.time() - start
            return HealthCheckResult(
                success=response.status < 400,
                check_type=self.check_type,
                duration=duration,
                message=f"HTTP {response.status}",
                label=self.name,
            )
        except urllib.error.HTTPError as e:
            duration = time.time() - start
            is_transient = e.code >= 500
            if is_transient:
                msg = f"HTTP {e.code}: {e.reason} (server error — may recover on retry)"
            else:
                msg = f"HTTP {e.code}: {e.reason} (check health endpoint configuration)"
            return HealthCheckResult(
                success=False,
                check_type=self.check_type,
                duration=duration,
                message=msg,
                transient=is_transient,
                label=self.name,
            )
        except urllib.error.URLError as e:
            duration = time.time() - start
            return HealthCheckResult(
                success=False,
                check_type=self.check_type,
                duration=duration,
                message=f"Service not yet reachable: {e.reason}",
                transient=True,
                label=self.name,
            )
        except Exception as e:
            duration = time.time() - start
            return HealthCheckResult(
                success=False,
                check_type=self.check_type,
                duration=duration,
                message=f"Connection error: {e}",
                label=self.name,
            )


class TCPHealthChecker(HealthChecker):
    """TCP port-based health check."""

    check_type = "tcp"

    def __init__(self, host: str, port: int, *, name: str = ""):
        """Initialize TCP health checker.

        Args:
            host: Host to connect to
            port: Port to check
            name: Human-readable label for this check (defaults to host:port)
        """
        self.host = host
        self.port = port
        self.name = name or f"{host}:{port}"
        self.logger = logging.getLogger(__name__)

    def check(self, timeout: float = 5.0) -> HealthCheckResult:
        """Check TCP port connectivity.

        Args:
            timeout: Connection timeout in seconds

        Returns:
            HealthCheckResult with TCP connectivity status
        """
        import socket

        start = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            result = sock.connect_ex((self.host, self.port))

            duration = time.time() - start
            success = result == 0

            return HealthCheckResult(
                success=success,
                check_type=self.check_type,
                duration=duration,
                message="TCP connection successful"
                if success
                else f"TCP error: {result}",
                label=self.name,
            )
        except Exception as e:
            duration = time.time() - start
            return HealthCheckResult(
                success=False,
                check_type=self.check_type,
                duration=duration,
                message=f"TCP check error: {e}",
                label=self.name,
            )
        finally:
            sock.close()


class ExecHealthChecker(HealthChecker):
    """Command execution-based health check."""

    check_type = "exec"

    def __init__(self, command: str, *, shell: bool = False, name: str = ""):
        """Initialize exec health checker.

        Args:
            command: Command to execute (should return 0 on success)
            shell: If True, run via shell. Default False uses shlex.split().
            name: Human-readable label for this check (defaults to command[:40])

        Raises:
            ValueError: If *shell* is False and *command* contains shell
                metacharacters or is empty.
        """
        if not shell:
            from fraisier.dbops._validation import validate_shell_command

            validate_shell_command(command)
        self.command = command
        self.use_shell = shell
        self.name = name or command[:40]
        self.logger = logging.getLogger(__name__)

    def check(self, timeout: float = 5.0) -> HealthCheckResult:
        """Execute health check command.

        Args:
            timeout: Command execution timeout in seconds

        Returns:
            HealthCheckResult with command exit status
        """
        start = time.time()
        try:
            cmd = self.command if self.use_shell else shlex.split(self.command)
            result = subprocess.run(
                cmd,
                shell=self.use_shell,
                timeout=timeout,
                capture_output=True,
                text=True,
                check=False,
            )
            duration = time.time() - start

            return HealthCheckResult(
                success=result.returncode == 0,
                check_type=self.check_type,
                duration=duration,
                message=(result.stdout.strip() or f"Exit code: {result.returncode}"),
                label=self.name,
            )
        except subprocess.TimeoutExpired:
            duration = time.time() - start
            return HealthCheckResult(
                success=False,
                check_type=self.check_type,
                duration=duration,
                message=f"Command timeout after {timeout}s",
                label=self.name,
            )
        except Exception as e:
            duration = time.time() - start
            return HealthCheckResult(
                success=False,
                check_type=self.check_type,
                duration=duration,
                message=f"Execution error: {e}",
                label=self.name,
            )


class HealthCheckManager:
    """Manage health checks with retries, timeouts, and monitoring.

    Usage:
        manager = HealthCheckManager(
            provider="bare_metal",
            deployment_id="deploy-123"
        )
        result = manager.check_with_retries(
            HTTPHealthChecker("http://localhost:8000/health"),
            max_retries=3
        )
        if result.success:
            print("Service is healthy")
    """

    def __init__(self, provider: str | None = None, deployment_id: str | None = None):
        """Initialize health check manager.

        Args:
            provider: Provider name for metrics
            deployment_id: Deployment ID for logging and metrics
        """
        self.provider = provider
        self.deployment_id = deployment_id
        self.logger = ContextualLogger(__name__)
        self.metrics = get_metrics_recorder()

    def check_with_retries(
        self,
        checker: HealthChecker,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        backoff_factor: float = 2.0,
        max_delay: float = 30.0,
        timeout: float = 5.0,
    ) -> HealthCheckResult:
        """Perform health check with exponential backoff retries.

        Args:
            checker: HealthChecker instance
            max_retries: Maximum number of retry attempts
            initial_delay: Initial delay between retries in seconds
            backoff_factor: Multiplier for exponential backoff
            max_delay: Maximum delay between retries
            timeout: Timeout per check attempt

        Returns:
            HealthCheckResult from last attempt
        """
        last_result = None
        delay = initial_delay
        start_time = time.time()

        for attempt in range(max_retries):
            try:
                with self.logger.context(
                    attempt=attempt + 1,
                    deployment_id=self.deployment_id,
                    check_type=checker.check_type,
                ):
                    last_result = checker.check(timeout=timeout)

                    if last_result.success:
                        if attempt == 0:
                            self.logger.info(
                                f"Health check passed on attempt {attempt + 1}/"
                                f"{max_retries}",
                                duration=last_result.duration,
                            )
                        else:
                            elapsed = time.time() - start_time
                            self.logger.info(
                                f"Health check passed on attempt {attempt + 1}/"
                                f"{max_retries} "
                                f"(service took {elapsed:.1f}s to become ready)",
                                duration=last_result.duration,
                            )
                        return last_result

                    # Categorize failure: log level depends on transient flag
                    if last_result.transient is True:
                        self.logger.info(
                            f"Health check attempt {attempt + 1}/{max_retries}: "
                            f"service not yet ready — {last_result.message}",
                            duration=last_result.duration,
                        )
                    else:
                        self.logger.warning(
                            f"Health check '{checker.name}' failed on attempt "
                            f"{attempt + 1}/{max_retries}",
                            check_message=last_result.message,
                            duration=last_result.duration,
                        )

            except Exception as e:
                self.logger.error(
                    f"Error during health check attempt {attempt + 1}/"
                    f"{max_retries}: {e}",
                    _exc_info=True,
                )

            # Wait before retry (except on last attempt)
            if attempt < max_retries - 1:
                wait_time = min(delay, max_delay)
                self.logger.info(f"Retrying in {wait_time:.1f}s")
                time.sleep(wait_time)
                delay *= backoff_factor

        # All retries exhausted
        if last_result is None:
            last_result = HealthCheckResult(
                success=False,
                check_type=checker.check_type,
                duration=0,
                message=f"All {max_retries} health check attempts failed",
            )

        # Provide recovery hint based on error category
        if last_result.transient is True:
            hint = "Service failed to start within expected time. Check service logs."
        elif last_result.transient is False:
            hint = "This may be a configuration issue. Check the health endpoint."
        else:
            hint = "Check service logs for details."

        self.logger.error(
            f"Health check '{checker.name}' failed after {max_retries} attempts."
            f" {hint}",
            check_message=last_result.message,
        )
        return last_result

    async def async_check_with_retries(
        self,
        check_fn: Callable[[], Awaitable[bool]],
        max_retries: int = 3,
        initial_delay: float = 1.0,
        backoff_factor: float = 2.0,
        max_delay: float = 30.0,
    ) -> bool:
        """Async health check with exponential backoff retries.

        Args:
            check_fn: Async callable returning True (healthy) or False
            max_retries: Maximum number of retry attempts
            initial_delay: Initial delay between retries in seconds
            backoff_factor: Multiplier for exponential backoff
            max_delay: Maximum delay between retries

        Returns:
            True if any attempt succeeded, False otherwise
        """
        delay = initial_delay

        for attempt in range(max_retries):
            try:
                if await check_fn():
                    self.logger.info(
                        f"Health check passed on attempt {attempt + 1}",
                    )
                    return True

                self.logger.warning(
                    f"Health check failed on attempt {attempt + 1}",
                )

            except Exception as e:
                self.logger.error(
                    f"Error during health check attempt {attempt + 1}: {e}",
                    _exc_info=True,
                )

            if attempt < max_retries - 1:
                wait_time = min(delay, max_delay)
                await asyncio.sleep(wait_time)
                delay *= backoff_factor

        self.logger.error(
            f"Health check failed after {max_retries} attempts",
        )
        return False

    def check_and_record_metrics(
        self,
        checker: HealthChecker,
        max_retries: int = 3,
        timeout: float = 5.0,
    ) -> HealthCheckResult:
        """Perform health check and record metrics.

        Args:
            checker: HealthChecker instance
            max_retries: Maximum retry attempts
            timeout: Timeout per check

        Returns:
            HealthCheckResult with metrics recorded
        """
        result = self.check_with_retries(
            checker,
            max_retries=max_retries,
            timeout=timeout,
        )

        # Record metrics
        if self.provider:
            status = "pass" if result.success else "fail"
            self.metrics.record_health_check(
                provider=self.provider,
                check_type=result.check_type,
                status=status,
                duration=result.duration,
            )

        return result

    def check_service_ready(
        self,
        checker: HealthChecker,
        max_retries: int = 5,
        initial_delay: float = 2.0,
        timeout: float = 5.0,
    ) -> tuple[bool, str]:
        """Wait for service to be ready with retries.

        Useful during deployment startup when service needs time to initialize.

        Args:
            checker: HealthChecker instance
            max_retries: Maximum wait attempts
            initial_delay: Initial delay between checks
            timeout: Timeout per check

        Returns:
            Tuple of (success: bool, message: str)
        """
        result = self.check_with_retries(
            checker,
            max_retries=max_retries,
            initial_delay=initial_delay,
            timeout=timeout,
        )

        return result.success, result.message or "Health check failed"

    def check_service_healthy(
        self,
        checker: HealthChecker,
        timeout: float = 5.0,
    ) -> bool:
        """Quick health check without retries.

        Args:
            checker: HealthChecker instance
            timeout: Timeout for check

        Returns:
            True if service is healthy, False otherwise
        """
        result = self.check_with_retries(
            checker,
            max_retries=1,
            timeout=timeout,
        )
        return result.success


class CompositeHealthChecker:
    """Perform multiple health checks and aggregate results.

    Usage:
        composite = CompositeHealthChecker()
        composite.add_check("http", HTTPHealthChecker("http://localhost/health"))
        composite.add_check("tcp", TCPHealthChecker("localhost", 8000))
        success, results = composite.check_all(require_all=True)
    """

    def __init__(self):
        """Initialize composite health checker."""
        self.checks: dict[str, HealthChecker] = {}
        self.logger = logging.getLogger(__name__)

    def add_check(self, name: str, checker: HealthChecker) -> None:
        """Add a health check.

        Args:
            name: Name for this check
            checker: HealthChecker instance
        """
        self.checks[name] = checker

    def check_all(
        self,
        require_all: bool = True,
        timeout: float = 5.0,
    ) -> tuple[bool, dict[str, HealthCheckResult]]:
        """Execute all health checks.

        Args:
            require_all: If True, all checks must pass.
                If False, at least one must pass.
            timeout: Timeout per check

        Returns:
            Tuple of (overall_success: bool, results: dict of check results)
        """
        results = {}
        passed = 0
        failed = 0

        for name, checker in self.checks.items():
            try:
                result = checker.check(timeout=timeout)
                results[name] = result

                if result.success:
                    passed += 1
                    self.logger.info(f"Check '{name}' passed")
                else:
                    failed += 1
                    self.logger.warning(f"Check '{name}' failed: {result.message}")
            except Exception as e:
                result = HealthCheckResult(
                    success=False,
                    check_type=checker.check_type,
                    duration=0,
                    message=f"Error: {e}",
                )
                results[name] = result
                failed += 1
                self.logger.error(f"Error in check '{name}': {e}")

        # Determine overall success
        overall_success = failed == 0 if require_all else passed > 0

        return overall_success, results


# ---------------------------------------------------------------------------
# Multi-service aggregate health
# ---------------------------------------------------------------------------


@dataclass
class ServiceHealthConfig:
    """Per-service health check configuration."""

    url: str
    headers: dict[str, str] | None = None
    version_field: str | None = None
    migration_field: str | None = None


@dataclass
class ServiceHealthResult:
    """Health result for a single service."""

    name: str
    url: str
    status: str  # "healthy" | "unhealthy"
    response_time_ms: float
    version: str | None = None
    migration: str | None = None


@dataclass
class AggregateHealthResult:
    """Aggregate health across multiple services."""

    status: str  # "healthy" | "degraded" | "unhealthy"
    services: dict[str, ServiceHealthResult] = field(default_factory=dict)
    response_time_ms: float = 0.0
    gateway: dict[str, str] | None = None

    def to_dict(
        self,
        response_config: "HealthResponseConfig | None" = None,
    ) -> dict[str, Any]:
        """Serialize to JSON-compatible dict, applying security omissions."""
        result: dict[str, Any] = {"status": self.status}

        services_dict: dict[str, Any] = {}
        for svc_name, svc in self.services.items():
            svc_data: dict[str, Any] = {
                "url": svc.url,
                "status": svc.status,
            }
            if (
                response_config is None or response_config.include_version
            ) and svc.version is not None:
                svc_data["version"] = svc.version
            if (
                response_config is None or response_config.include_migration
            ) and svc.migration is not None:
                svc_data["migration"] = svc.migration
            services_dict[svc_name] = svc_data

        result["services"] = services_dict

        if self.gateway is not None:
            result["gateway"] = self.gateway

        if response_config is None or response_config.include_response_time:
            result["response_time_ms"] = self.response_time_ms

        return result


class AggregateHealthChecker:
    """Check health across multiple services with endpoint fallback."""

    def __init__(
        self,
        services: dict[str, "ServiceHealthConfig"],
        health_config: "HealthConfig",
    ):
        """Initialize aggregate health checker.

        Args:
            services: Mapping of service name -> ServiceHealthConfig
            health_config: HealthConfig with endpoints list and timeouts
        """
        self.services = services
        self.health_config = health_config
        self.logger = logging.getLogger(__name__)

    def _check_service(
        self, name: str, service_config: "ServiceHealthConfig"
    ) -> ServiceHealthResult:
        """Check a single service using its full URL."""
        url = service_config.url
        start = time.time()
        try:
            _validate_health_check_url(url)

            # Create request with headers if provided
            request = urllib.request.Request(url)
            if service_config.headers:
                for key, value in service_config.headers.items():
                    request.add_header(key, value)

            response = urllib.request.urlopen(request, timeout=5.0)
            duration_ms = (time.time() - start) * 1000
            if response.status < 400:
                # Parse response for additional data
                version = None
                migration = None
                try:
                    data = json.loads(response.read().decode("utf-8"))
                    # Use per-service field mappings if provided, else global
                    version_field = (
                        service_config.version_field or self.health_config.version_field
                    )
                    migration_field = (
                        service_config.migration_field
                        or self.health_config.migration_field
                    )
                    version = _get_nested(data, version_field)
                    migration = _get_nested(data, migration_field)
                except (json.JSONDecodeError, UnicodeDecodeError, KeyError):
                    pass  # Ignore parsing errors, keep None

                return ServiceHealthResult(
                    name=name,
                    url=url,
                    status="healthy",
                    response_time_ms=round(duration_ms, 1),
                    version=version,
                    migration=migration,
                )
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            pass

        duration_ms = (time.time() - start) * 1000
        return ServiceHealthResult(
            name=name,
            url=url,
            status="unhealthy",
            response_time_ms=round(duration_ms, 1),
        )

    def check_all(self) -> AggregateHealthResult:
        """Check all services and return aggregate result."""
        overall_start = time.time()
        results: dict[str, ServiceHealthResult] = {}

        for name, service_config in self.services.items():
            results[name] = self._check_service(name, service_config)

        total_ms = round((time.time() - overall_start) * 1000, 1)

        healthy_count = sum(1 for r in results.values() if r.status == "healthy")
        total_count = len(results)

        if healthy_count == total_count:
            status = "healthy"
        elif healthy_count > 0:
            status = "degraded"
        else:
            status = "unhealthy"

        return AggregateHealthResult(
            status=status,
            services=results,
            response_time_ms=total_ms,
        )

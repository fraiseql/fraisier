"""Deployment providers for multiple infrastructure types.

Supports:
- Bare Metal (SSH + systemd)
- Docker Compose
"""

from .base import (
    DeploymentProvider,
    HealthCheck,
    HealthCheckType,
    ProviderConfig,
    ProviderRegistry,
    ProviderStatus,
    ProviderType,
)

__all__ = [
    "BareMetalProvider",
    "DeploymentProvider",
    "DockerComposeProvider",
    "HealthCheck",
    "HealthCheckType",
    "ProviderConfig",
    "ProviderRegistry",
    "ProviderStatus",
    "ProviderType",
]


def __getattr__(name):
    """Lazy import providers to avoid pulling in optional deps at import time."""
    if name == "BareMetalProvider":
        from .bare_metal import BareMetalProvider

        return BareMetalProvider
    elif name == "DockerComposeProvider":
        from .docker_compose import DockerComposeProvider

        return DockerComposeProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

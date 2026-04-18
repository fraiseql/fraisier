# Service managers package

import platform
from typing import TYPE_CHECKING, Any

from .base import ServiceManager
from .rc import RcServiceManager
from .systemd import SystemdServiceManager

if TYPE_CHECKING:  # pragma: no cover
    from fraisier.runners import CommandRunner


def get_service_manager(
    runner: "CommandRunner", config: dict[str, Any] | None = None
) -> ServiceManager:
    """Get the appropriate ServiceManager for the current platform.

    Args:
        runner: CommandRunner instance for executing commands.
        config: Optional config dict with 'service_manager' key.

    Returns:
        ServiceManager instance.

    Raises:
        ValueError: If platform is unsupported or service_manager is invalid.
    """
    config = config or {}
    service_manager_type = config.get("service_manager")

    if service_manager_type:
        # Explicit config override
        if service_manager_type == "systemd":
            return SystemdServiceManager(runner)
        elif service_manager_type == "rc":
            return RcServiceManager(runner)
        else:
            raise ValueError(
                f"Invalid service_manager: {service_manager_type!r}. "
                "Must be 'systemd' or 'rc'."
            )

    # Auto-detect based on platform
    system = platform.system()
    if system == "Linux":
        return SystemdServiceManager(runner)
    elif system == "FreeBSD":
        return RcServiceManager(runner)
    else:
        raise ValueError(
            f"Unsupported platform: {system}. "
            "Supported platforms: Linux (systemd), FreeBSD (rc)."
        )

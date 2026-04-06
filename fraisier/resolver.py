"""Unified configuration resolution from environment variables and YAML.

Centralises all os.getenv calls into a single ConfigResolver that can be
injected for testing (hermetic tests without monkeypatching os.environ).
"""

import os
from collections.abc import Mapping
from pathlib import Path

from fraisier.config import ConfigurationError

# Default database path if /var/lib/fraisier exists
DEFAULT_DB_PATH = Path("/var/lib/fraisier/fraisier.db")


def _config_search_locations() -> list[Path]:
    """Return config search locations, evaluated lazily so CWD is current."""
    return [
        Path.cwd() / "fraises.yaml",
        Path.cwd() / "config" / "fraises.yaml",
        Path("/opt/fraisier/fraises.yaml"),
        Path(__file__).parent.parent / "fraises.yaml",
    ]


class ConfigResolver:
    """Unified resolver for environment variables with fallbacks.

    Accepts an optional environ dict for dependency injection (testing).
    If not provided, uses os.environ.

    This enables hermetic tests where env vars are passed explicitly,
    and production code where os.environ is used automatically.
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        """Initialize resolver.

        Args:
            environ: Environment dict. If None, uses os.environ.
        """
        self._env = environ if environ is not None else os.environ

    @property
    def db_path(self) -> Path:
        """Resolve database path.

        Priority:
        1. FRAISIER_DB_PATH env var
        2. /var/lib/fraisier/fraisier.db if it exists
        3. Package directory fallback

        Returns:
            Path to fraisier database
        """
        if env_path := self._env.get("FRAISIER_DB_PATH"):
            return Path(env_path)
        if DEFAULT_DB_PATH.parent.exists():
            return DEFAULT_DB_PATH
        return Path(__file__).parent.parent / "fraisier.db"

    @property
    def config_path(self) -> Path:
        """Resolve config file path.

        Priority:
        1. FRAISIER_CONFIG env var (must exist)
        2. Search standard locations (return first existing file)
        3. Raise ConfigurationError if none found

        Returns:
            Path to fraises.yaml

        Raises:
            ConfigurationError: If no config file found in any location
        """
        # Check env var first
        if env_path := self._env.get("FRAISIER_CONFIG"):
            return Path(env_path)

        # Search standard locations
        for loc in _config_search_locations():
            if loc.exists():
                return loc

        # Nothing found
        locations_str = [str(p) for p in _config_search_locations()]
        raise ConfigurationError(f"fraises.yaml not found in any of: {locations_str}")

    @property
    def log_level(self) -> str:
        """Resolve logging level.

        Priority:
        1. FRAISIER_LOG_LEVEL env var
        2. Default: "INFO"

        Returns:
            Log level string (e.g., "DEBUG", "INFO", "WARNING")
        """
        return self._env.get("FRAISIER_LOG_LEVEL", "INFO")

    @property
    def webhook_host(self) -> str:
        """Resolve webhook server host.

        Priority:
        1. FRAISIER_WEBHOOK_HOST env var
        2. Default: "0.0.0.0" (listen on all interfaces)

        Returns:
            Host IP or hostname
        """
        return self._env.get("FRAISIER_WEBHOOK_HOST", "0.0.0.0")

    @property
    def webhook_port(self) -> int:
        """Resolve webhook server port.

        Priority:
        1. FRAISIER_WEBHOOK_PORT env var
        2. Default: 8080

        Returns:
            Port number as integer

        Raises:
            ValueError: If env var is not a valid integer
        """
        port_str = self._env.get("FRAISIER_WEBHOOK_PORT", "8080")
        return int(port_str)

    @property
    def systemctl_socket(self) -> str | None:
        """Resolve systemctl helper Unix socket path.

        Priority:
        1. FRAISIER_SYSTEMCTL_SOCKET env var
        2. None if unset

        Returns:
            Absolute path to the Unix domain socket or None
        """
        return self._env.get("FRAISIER_SYSTEMCTL_SOCKET")

    @property
    def systemctl_wrapper(self) -> str | None:
        """Resolve custom systemctl wrapper.

        Priority:
        1. FRAISIER_SYSTEMCTL_WRAPPER env var
        2. None if unset

        Returns:
            Path to wrapper script or None
        """
        return self._env.get("FRAISIER_SYSTEMCTL_WRAPPER")

    @property
    def git_provider(self) -> str | None:
        """Resolve git provider override.

        Priority:
        1. FRAISIER_GIT_PROVIDER env var
        2. None if unset (let git config decide)

        Returns:
            Provider name (e.g., "github", "gitlab") or None
        """
        return self._env.get("FRAISIER_GIT_PROVIDER")

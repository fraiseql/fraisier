"""Framework configuration system with TOML files and environment overrides.

Supports precedence: flags > user config > system config > defaults
"""

import os
from pathlib import Path
from typing import Any, ClassVar

try:
    import tomllib
except ImportError:
    import tomli as tomllib


class FrameworkConfig:
    """Framework configuration with hierarchical loading."""

    DEFAULTS: ClassVar[dict[str, Any]] = {
        "logging": {
            "level": "INFO",
            "format": "text",
        },
        "timeout": 300,
        "retries": 3,
    }

    def __init__(self, data: dict[str, Any]):
        """Initialize config with data dict."""
        self._data = data

    @classmethod
    def load(
        cls,
        system_config_path: Path | None = None,
        user_config_path: Path | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> "FrameworkConfig":
        """Load configuration with precedence.

        Precedence (highest to lowest):
        1. overrides (from flags)
        2. user config
        3. system config
        4. defaults
        """
        data = cls._load_defaults()

        # Load system config
        if system_config_path and system_config_path.exists():
            system_data = cls._load_toml(system_config_path)
            cls._deep_merge(data, system_data)

        # Load user config
        if user_config_path and user_config_path.exists():
            user_data = cls._load_toml(user_config_path)
            cls._deep_merge(data, user_data)

        # Apply environment overrides
        env_overrides = cls._load_env_overrides()
        cls._deep_merge(data, env_overrides)

        # Apply flag overrides
        if overrides:
            processed_overrides = cls._process_dotted_overrides(overrides)
            cls._deep_merge(data, processed_overrides)

        return cls(data)

    @classmethod
    def _load_defaults(cls) -> dict[str, Any]:
        """Load default configuration."""
        return cls.DEFAULTS.copy()

    @classmethod
    def _load_toml(cls, path: Path) -> dict[str, Any]:
        """Load TOML file."""
        with path.open("rb") as f:
            return tomllib.load(f)

    @classmethod
    def _load_env_overrides(cls) -> dict[str, Any]:
        """Load configuration from environment variables.

        Environment variables should be prefixed with FRAISIER_ and use
        double underscores for nesting, e.g. FRAISIER_LOGGING__LEVEL=DEBUG
        """
        overrides = {}
        prefix = "FRAISIER_"

        for env_key, env_value in os.environ.items():
            if not env_key.startswith(prefix):
                continue

            # Remove prefix and split on double underscore
            config_key = env_key[len(prefix) :].lower()
            key_parts = config_key.split("__")

            # Build nested dict
            current = overrides
            for part in key_parts[:-1]:
                current = current.setdefault(part, {})
            current[key_parts[-1]] = env_value

        return overrides

    @classmethod
    def _process_dotted_overrides(cls, overrides: dict[str, Any]) -> dict[str, Any]:
        """Convert dotted keys to nested dict structure."""
        result = {}
        for dotted_key, value in overrides.items():
            keys = dotted_key.split(".")
            current = result
            for key in keys[:-1]:
                current = current.setdefault(key, {})
            current[keys[-1]] = value
        return result

    @classmethod
    def _deep_merge(cls, base: dict[str, Any], update: dict[str, Any]) -> None:
        """Deep merge update dict into base dict."""
        for key, value in update.items():
            if isinstance(value, dict) and key in base and isinstance(base[key], dict):
                cls._deep_merge(base[key], value)
            else:
                base[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by dot-separated key."""
        keys = key.split(".")
        value = self._data
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default

    def to_dict(self) -> dict[str, Any]:
        """Return configuration as dict."""
        return self._data.copy()

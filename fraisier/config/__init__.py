"""Fraisier configuration module.

This package splits the monolithic config.py into:
- schema.py: Dataclass definitions and constants
- loader.py: FraisierConfig class and loading logic
- validators.py: Validation logic (future)

All public APIs are re-exported here for backwards compatibility.
"""

from fraisier.config._lazy_env import LazyEnv, is_string_like, to_str
from fraisier.config.loader import (
    FraisierConfig,
    _config,  # noqa: F401 — exposed for test singleton reset
    _config_lock,  # noqa: F401 — exposed for test singleton reset
    get_config,
    reset_config,
    resolve_config_path,
)
from fraisier.config.schema import (
    CONFIG_SEARCH_LOCATIONS,
    DEFAULT_SECURITY,
    PG_LOG_ENV_DEFAULTS,
    SECURITY_DIRECTIVE_MAP,
    AuditHookConfig,
    BackupHookConfig,
    DeploymentConfig,
    DiscordHookConfig,
    EmailHookConfig,
    GenericNotificationHookConfig,
    GithubActionsScaffoldConfig,
    HealthConfig,
    HealthResponseConfig,
    MigrationHooksConfig,
    NginxEnvConfig,
    NginxScaffoldConfig,
    NotificationHooksConfig,
    PostgresLoggingConfig,
    PreflightConfig,
    RestrictedPath,
    ScaffoldConfig,
    ServiceConfig,
    ShipCheckConfig,
    ShipConfig,
    SlackHookConfig,
    SystemdScaffoldConfig,
    TeamsHookConfig,
)

# Import from errors (for re-export)
from fraisier.errors import ConfigurationError, ValidationError

__all__ = [
    "CONFIG_SEARCH_LOCATIONS",
    "DEFAULT_SECURITY",
    "PG_LOG_ENV_DEFAULTS",
    "SECURITY_DIRECTIVE_MAP",
    "AuditHookConfig",
    "BackupHookConfig",
    "ConfigurationError",
    "DeploymentConfig",
    "DiscordHookConfig",
    "EmailHookConfig",
    "FraisierConfig",
    "GenericNotificationHookConfig",
    "GithubActionsScaffoldConfig",
    "HealthConfig",
    "HealthResponseConfig",
    "LazyEnv",
    "MigrationHooksConfig",
    "NginxEnvConfig",
    "NginxScaffoldConfig",
    "NotificationHooksConfig",
    "PostgresLoggingConfig",
    "PreflightConfig",
    "RestrictedPath",
    "ScaffoldConfig",
    "ServiceConfig",
    "ShipCheckConfig",
    "ShipConfig",
    "SlackHookConfig",
    "SystemdScaffoldConfig",
    "TeamsHookConfig",
    "ValidationError",
    "get_config",
    "is_string_like",
    "reset_config",
    "resolve_config_path",
    "to_str",
]

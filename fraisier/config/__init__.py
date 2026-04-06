"""Fraisier configuration module.

This package splits the monolithic config.py into:
- schema.py: Dataclass definitions and constants
- loader.py: FraisierConfig class and loading logic
- validators.py: Validation logic (future)

All public APIs are re-exported here for backwards compatibility.
"""

# Import from schema module
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
    RestrictedPath,
    ScaffoldConfig,
    ServiceConfig,
    ShipCheckConfig,
    ShipConfig,
    SlackHookConfig,
    SystemdScaffoldConfig,
    TeamsHookConfig,
)

# Import from loader module
from fraisier.config.loader import (
    FraisierConfig,
    _config,
    _config_lock,
    get_config,
    reset_config,
)

# Import from errors (for re-export)
from fraisier.errors import ConfigurationError, ValidationError

__all__ = [
    # Errors
    "ConfigurationError",
    "ValidationError",
    # Constants
    "CONFIG_SEARCH_LOCATIONS",
    "DEFAULT_SECURITY",
    "SECURITY_DIRECTIVE_MAP",
    "PG_LOG_ENV_DEFAULTS",
    # Dataclasses
    "ServiceConfig",
    "RestrictedPath",
    "NginxEnvConfig",
    "SystemdScaffoldConfig",
    "NginxScaffoldConfig",
    "GithubActionsScaffoldConfig",
    "PostgresLoggingConfig",
    "ScaffoldConfig",
    "DeploymentConfig",
    "HealthResponseConfig",
    "HealthConfig",
    "ShipCheckConfig",
    "BackupHookConfig",
    "AuditHookConfig",
    "SlackHookConfig",
    "DiscordHookConfig",
    "TeamsHookConfig",
    "EmailHookConfig",
    "GenericNotificationHookConfig",
    "NotificationHooksConfig",
    "MigrationHooksConfig",
    "ShipConfig",
    # Loader functions and classes
    "FraisierConfig",
    "get_config",
    "reset_config",
]

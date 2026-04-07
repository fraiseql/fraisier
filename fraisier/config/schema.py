"""Configuration schema: dataclasses and constants for Fraisier.

This module contains all dataclass definitions and configuration constants.
It has no validation logic — just schema definitions.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fraisier.errors import ValidationError

_GIT_URL_RE = re.compile(
    r"^("
    r"https?://[^\s]+"
    r"|git@[\w.\-]+:[\w./-]+"
    r"|ssh://[^\s]+"
    r"|/[\w./-]+"
    r")$"
)

_VALID_STRATEGIES = {"rebuild", "restore_migrate", "migrate", "apply"}
_DEFAULT_TIMEOUT = 600  # 10 minutes


# Standard locations to search for fraises.yaml configuration file
def _config_search_locations() -> list[Path]:
    """Return config search locations, evaluated lazily so CWD is current."""
    return [
        Path.cwd() / "fraises.yaml",
        Path.cwd() / "config" / "fraises.yaml",
        Path("/opt/fraisier/fraises.yaml"),
        Path(__file__).parent.parent.parent / "fraises.yaml",
    ]


# Kept for backward compatibility (used by daemon.py for display purposes).
CONFIG_SEARCH_LOCATIONS = _config_search_locations()
_UNIT_NAME_RE = re.compile(r"^[a-zA-Z0-9._\-@\\]+$")

# snake_case -> systemd PascalCase mapping for security directives
SECURITY_DIRECTIVE_MAP: dict[str, str] = {
    "no_new_privileges": "NoNewPrivileges",
    "protect_system": "ProtectSystem",
    "protect_home": "ProtectHome",
    "private_tmp": "PrivateTmp",
    "private_devices": "PrivateDevices",
    "protect_kernel_tunables": "ProtectKernelTunables",
    "protect_kernel_modules": "ProtectKernelModules",
    "protect_control_groups": "ProtectControlGroups",
    "restrict_address_families": "RestrictAddressFamilies",
    "system_call_filter": "SystemCallFilter",
    "protect_clock": "ProtectClock",
    "restrict_namespaces": "RestrictNamespaces",
    "restrict_realtime": "RestrictRealtime",
    "restrict_suid_sgid": "RestrictSUIDSGID",
    "lock_personality": "LockPersonality",
    "memory_deny_write_execute": "MemoryDenyWriteExecute",
    "remove_ipc": "RemoveIPC",
    "private_users": "PrivateUsers",
    "protect_hostname": "ProtectHostname",
    "protect_kernel_logs": "ProtectKernelLogs",
}

DEFAULT_SECURITY: dict[str, str | bool] = {
    "no_new_privileges": True,
    "protect_system": "strict",
    "protect_home": True,
    "private_tmp": True,
    "private_devices": True,
    "protect_kernel_tunables": True,
    "protect_kernel_modules": True,
    "protect_control_groups": True,
    "restrict_address_families": "AF_INET AF_INET6 AF_UNIX",
    "system_call_filter": "~@clock @debug @module @mount @obsolete @reboot @swap",
}

# Valid memory size pattern (e.g., "4G", "512M", "2T")
_MEMORY_SIZE_RE = re.compile(r"^\d+[KMGT]$")

_VALID_SERVICE_TYPES = {
    "simple",
    "exec",
    "forking",
    "oneshot",
    "dbus",
    "notify",
    "notify-reload",
    "idle",
}


@dataclass
class ServiceConfig:
    """Per-environment systemd service configuration."""

    service_name: str | None = None
    user: str | None = None
    group: str | None = None
    port: int | None = None
    workers: int = 1
    exec: str | None = None
    type: str = "notify"
    exec_start_pre: list[str] = field(default_factory=list)
    memory_max: str | None = None
    memory_high: str | None = None
    cpu_quota: str | None = None
    runtime_directory: str | None = None
    runtime_directory_mode: str | None = None
    logs_directory: str | None = None
    logs_directory_mode: str | None = None
    environment_file: str | None = None
    credentials: dict[str, str] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    security: dict[str, str | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.service_name is not None and not _UNIT_NAME_RE.match(self.service_name):
            raise ValidationError(
                f"service.service_name contains invalid characters: "
                f"{self.service_name!r}",
            )
        if self.port is not None and not (1 <= self.port <= 65535):
            raise ValidationError(
                f"service.port must be 1-65535, got {self.port}",
            )
        if self.type not in _VALID_SERVICE_TYPES:
            raise ValidationError(
                f"service.type must be one of {sorted(_VALID_SERVICE_TYPES)}, "
                f"got {self.type!r}",
            )
        for size_field in ("memory_max", "memory_high"):
            val = getattr(self, size_field)
            if val is not None and not _MEMORY_SIZE_RE.match(val):
                raise ValidationError(
                    f"service.{size_field} must match \\d+[KMGT], got {val!r}",
                )
        for cred_name, cred_path in self.credentials.items():
            if not cred_path.startswith("/"):
                raise ValidationError(
                    f"service.credentials.{cred_name} must be an absolute path, "
                    f"got {cred_path!r}",
                )
        # Reject newlines in environment variable names and values — they
        # would inject extra directives into the rendered systemd unit.
        for key, val in self.environment.items():
            if "\n" in key:
                raise ValidationError(
                    f"Newline in environment variable name: {key!r}",
                )
            if "\n" in str(val):
                raise ValidationError(
                    f"Newline in environment variable value for {key!r}",
                )
        # Validate exec command to prevent shell metacharacter injection
        if self.exec is not None:
            _SHELL_META_RE = re.compile(r"[;|&`$()]")
            if _SHELL_META_RE.search(self.exec):
                raise ValidationError(
                    f"Shell metacharacter detected in service.exec: {self.exec!r}",
                )

    @property
    def resolved_security(self) -> dict[str, str | bool]:
        """Return merged security directives (user overrides on top of defaults)."""
        merged = {**DEFAULT_SECURITY}
        merged.update(self.security)
        return merged

    @classmethod
    def from_env_dict(cls, env: dict[str, Any]) -> "ServiceConfig":
        """Parse ServiceConfig from an environment dict.

        Supports both nested ``service:`` key and legacy flat fields.
        The nested ``service:`` key takes precedence.
        """
        svc = env.get("service", {}) or {}

        # Legacy flat-field mapping (only used when service: key doesn't set them)
        def _get(key: str, legacy_key: str | None = None, default: Any = None) -> Any:
            val = svc.get(key)
            if val is not None:
                return val
            if legacy_key:
                val = env.get(legacy_key)
                if val is not None:
                    return val
            return default

        return cls(
            service_name=svc.get("service_name"),
            user=svc.get("user"),
            group=svc.get("group"),
            port=_get("port"),
            workers=_get("workers", "worker_count", 1),
            exec=_get("exec", "exec_command"),
            type=svc.get("type", "notify"),
            exec_start_pre=svc.get("exec_start_pre", []),
            memory_max=_get("memory_max", "memory_max"),
            memory_high=svc.get("memory_high"),
            cpu_quota=svc.get("cpu_quota"),
            runtime_directory=svc.get("runtime_directory"),
            runtime_directory_mode=svc.get("runtime_directory_mode"),
            logs_directory=svc.get("logs_directory"),
            logs_directory_mode=svc.get("logs_directory_mode"),
            environment_file=svc.get("environment_file"),
            credentials=svc.get("credentials", {}),
            environment=svc.get("environment", {}),
            security=svc.get("security", {}),
        )


@dataclass
class RestrictedPath:
    """Nginx restricted path with allow/deny rules."""

    path: str
    allow: list[str] = field(default_factory=lambda: ["127.0.0.1"])
    deny: str = "all"


def _escape_cors_dots(origin: str) -> str:
    """Escape unescaped literal dots in a CORS origin for nginx regex.

    Dots that are already escaped (``\\.``) or part of regex
    metachar sequences (e.g. ``.*``, ``.+``) are left untouched.
    """
    # Match dots not preceded by backslash and not followed by regex quantifiers
    return re.sub(r"(?<!\\)\.(?![*+?])", r"\\.", origin)


@dataclass
class NginxEnvConfig:
    """Per-environment nginx configuration."""

    server_name: str | None = None
    ssl_cert: str | None = None
    ssl_key: str | None = None
    cors_origins: list[str] = field(default_factory=list)
    restricted_paths: list[RestrictedPath] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.ssl_cert and not self.ssl_key:
            raise ValidationError(
                "nginx.ssl_cert requires nginx.ssl_key to also be set",
            )
        if self.ssl_key and not self.ssl_cert:
            raise ValidationError(
                "nginx.ssl_key requires nginx.ssl_cert to also be set",
            )
        # Auto-escape CORS origins for nginx regex
        self.cors_origins = [_escape_cors_dots(o) for o in self.cors_origins]

    @property
    def cors_origins_escaped(self) -> list[str]:
        """Return CORS origins with literal dots escaped for nginx regex."""
        return [_escape_cors_dots(o) for o in self.cors_origins]

    @classmethod
    def from_env_dict(cls, env: dict[str, Any]) -> "NginxEnvConfig | None":
        """Parse NginxEnvConfig from an environment dict.

        Returns None if no ``nginx:`` key is present.
        """
        raw = env.get("nginx")
        if not raw or not isinstance(raw, dict):
            return None

        restricted = []
        for item in raw.get("restricted_paths", []):
            if isinstance(item, str):
                restricted.append(RestrictedPath(path=item))
            elif isinstance(item, dict):
                restricted.append(
                    RestrictedPath(
                        path=item["path"],
                        allow=item.get("allow", ["127.0.0.1"]),
                        deny=item.get("deny", "all"),
                    )
                )

        return cls(
            server_name=raw.get("server_name"),
            ssl_cert=raw.get("ssl_cert"),
            ssl_key=raw.get("ssl_key"),
            cors_origins=raw.get("cors_origins", []),
            restricted_paths=restricted,
        )


@dataclass
class SystemdScaffoldConfig:
    """Systemd scaffold options."""

    security_hardening: bool = True
    memory_max_default: str = "4G"


@dataclass
class NginxScaffoldConfig:
    """Nginx scaffold options."""

    ssl_provider: str = "letsencrypt"
    cors_origins: list[str] = field(default_factory=list)
    rate_limit: str = "10r/s"
    restricted_paths: list[str] = field(default_factory=list)
    webhook_port: int = 8080

    def __post_init__(self) -> None:
        # Auto-escape CORS origins for nginx regex
        self.cors_origins = [_escape_cors_dots(o) for o in self.cors_origins]

    @property
    def cors_origins_escaped(self) -> list[str]:
        """Return CORS origins (already escaped)."""
        return self.cors_origins


@dataclass
class GithubActionsScaffoldConfig:
    """GitHub Actions scaffold options."""

    python_versions: list[str] = field(default_factory=lambda: ["3.12"])
    test_command: str = "uv run pytest"
    lint_command: str = "uv run ruff check"
    format_command: str = "uv run ruff format --check"


@dataclass
class PostgresLoggingConfig:
    """PostgreSQL logging configuration for conf.d snippet."""

    log_min_duration_statement: str | None = None
    log_statement: str | None = None
    log_connections: bool | None = None
    log_line_prefix: str = "'%m [%p] %q%u@%d '"
    log_min_error_statement: str = "error"
    log_error_verbosity: str = "default"
    deadlock_timeout: str = "1s"
    log_lock_waits: bool = True
    log_rotation_age: str = "1d"
    log_rotation_size: str = "100MB"


PG_LOG_ENV_DEFAULTS: dict[str, dict[str, str | bool]] = {
    "development": {
        "log_min_duration_statement": "100",
        "log_statement": "all",
        "log_connections": True,
    },
    "staging": {
        "log_min_duration_statement": "250",
        "log_statement": "ddl",
        "log_connections": False,
    },
    "production": {
        "log_min_duration_statement": "500",
        "log_statement": "ddl",
        "log_connections": False,
    },
}


@dataclass
class ScaffoldConfig:
    """Parsed scaffold: section from fraises.yaml."""

    output_dir: str = "scripts/generated"
    deploy_user: str = "fraisier"
    config_path: str = "/opt/fraisier/fraises.yaml"
    deploy_environment_file: str | None = None
    template_dir: str | None = None
    socket_user: str = "www-data"
    socket_group: str = "www-data"
    systemd: SystemdScaffoldConfig = field(default_factory=SystemdScaffoldConfig)
    nginx: NginxScaffoldConfig = field(default_factory=NginxScaffoldConfig)
    github_actions: GithubActionsScaffoldConfig = field(
        default_factory=GithubActionsScaffoldConfig
    )
    postgresql: PostgresLoggingConfig = field(default_factory=PostgresLoggingConfig)

    @property
    def config_dir(self) -> str:
        """Parent directory of config_path."""
        return str(Path(self.config_path).parent)


_VALID_LOCK_BACKENDS = {"file", "database"}


@dataclass
class DeploymentConfig:
    """Parsed deployment: section from fraises.yaml."""

    lock_dir: str = "/run/fraisier"
    lock_backend: str = "file"
    lock_db_path: str = "/var/lib/fraisier/locks.db"
    status_file: str = "deployment_status.json"
    deploy_user: str = "fraisier"
    strategies: dict[str, str] = field(default_factory=dict)
    timeouts: dict[str, int] = field(default_factory=dict)

    def get_strategy(self, environment: str) -> str | None:
        """Get deployment strategy for an environment."""
        return self.strategies.get(environment)

    def get_timeout(self, environment: str) -> int:
        """Get timeout for an environment, with fallback to default."""
        return self.timeouts.get(environment, _DEFAULT_TIMEOUT)


@dataclass
class HealthResponseConfig:
    """Security omission rules for health response."""

    include_version: bool = True
    include_schema_hash: bool = True
    include_response_time: bool = True
    include_database: bool = False
    include_environment: bool = False
    include_commit: bool = False
    include_migration: bool = False


@dataclass
class HealthConfig:
    """Parsed health: section from fraises.yaml."""

    startup_timeout_seconds: int = 120
    deploy_poll_interval_seconds: int = 5
    endpoints: list[str] = field(default_factory=lambda: ["/health"])
    response: HealthResponseConfig = field(default_factory=HealthResponseConfig)
    version_field: str = "version"
    migration_field: str = "migration"


@dataclass
class ShipCheckConfig:
    """A single check in the ship pipeline."""

    name: str
    command: list[str]
    phase: str  # "fix", "validate", "test"
    triggers: list[str] | None = None
    timeout: int = 60


@dataclass
class BackupHookConfig:
    """Configuration for pre-migration backup hook."""

    enabled: bool = False
    backup_dir: str = "/var/backups/fraisier"
    retention_days: int = 30
    compress: bool = True


@dataclass
class AuditHookConfig:
    """Configuration for post-migration audit hook."""

    enabled: bool = False
    audit_dir: str = "/var/log/fraisier/audit"


@dataclass
class SlackHookConfig:
    """Configuration for Slack notification hook."""

    enabled: bool = False
    webhook_url: str = ""
    channel: str = "#deployments"
    mention_on_failure: str = ""  # e.g., "@engineering"


@dataclass
class DiscordHookConfig:
    """Configuration for Discord notification hook."""

    enabled: bool = False
    webhook_url: str = ""
    mention_on_failure: str = ""  # e.g., "@engineering"


@dataclass
class TeamsHookConfig:
    """Configuration for Microsoft Teams notification hook."""

    enabled: bool = False
    webhook_url: str = ""
    mention_on_failure: str = ""  # e.g., "@engineering"


@dataclass
class EmailHookConfig:
    """Configuration for email notification hook."""

    enabled: bool = False
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_email: str = ""
    to_emails: list[str] = field(default_factory=list)
    subject_prefix: str = "[Fraisier]"


@dataclass
class GenericNotificationHookConfig:
    """Configuration for custom notification hooks."""

    type: str = ""  # e.g., "slack", "discord", "teams", "email", "custom"
    enabled: bool = False
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class NotificationHooksConfig:
    """Configuration for multiple notification hooks."""

    slack: SlackHookConfig = field(default_factory=SlackHookConfig)
    discord: DiscordHookConfig = field(default_factory=DiscordHookConfig)
    teams: TeamsHookConfig = field(default_factory=TeamsHookConfig)
    email: EmailHookConfig = field(default_factory=EmailHookConfig)
    custom: list[GenericNotificationHookConfig] = field(default_factory=list)


@dataclass
class MigrationHooksConfig:
    """Configuration for migration hooks."""

    backup: BackupHookConfig = field(default_factory=BackupHookConfig)
    audit: AuditHookConfig = field(default_factory=AuditHookConfig)
    notifications: NotificationHooksConfig = field(
        default_factory=NotificationHooksConfig
    )


@dataclass
class ShipConfig:
    """Parsed ship: section from fraises.yaml."""

    checks: list[ShipCheckConfig] = field(default_factory=list)
    pr_base: str | None = None
    parallel: bool = True

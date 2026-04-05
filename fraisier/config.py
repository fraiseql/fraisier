"""Configuration loader for Fraisier deployment system.

Loads fraise definitions from fraises.yaml.
Supports hierarchical fraise -> environment structure.
"""

import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

from fraisier.errors import ConfigurationError, ValidationError

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
        Path(__file__).parent.parent / "fraises.yaml",
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
    systemd: SystemdScaffoldConfig = field(default_factory=SystemdScaffoldConfig)
    nginx: NginxScaffoldConfig = field(default_factory=NginxScaffoldConfig)
    github_actions: GithubActionsScaffoldConfig = field(
        default_factory=GithubActionsScaffoldConfig
    )
    postgresql: PostgresLoggingConfig = field(default_factory=PostgresLoggingConfig)


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


@dataclass
class HealthConfig:
    """Parsed health: section from fraises.yaml."""

    startup_timeout_seconds: int = 120
    deploy_poll_interval_seconds: int = 5
    endpoints: list[str] = field(default_factory=lambda: ["/health"])
    response: HealthResponseConfig = field(default_factory=HealthResponseConfig)


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


class FraisierConfig:
    """Load and manage deployment configuration from fraises.yaml.

    Supports hierarchical structure:
        fraises:
          <fraise_name>:
            type: api|etl|scheduled|backup
            environments:
              <env_name>:
                <config>
    """

    def __init__(self, config_path: Path | str | None = None):
        """Initialize configuration.

        Args:
            config_path: Path to fraises.yaml. If None, uses default locations.
        """
        self.config_path = self._resolve_config_path(config_path)
        self._config: dict[str, Any] = {}
        self._load()

    def _resolve_config_path(self, config_path: Path | str | None) -> Path:
        """Resolve configuration file path."""
        if config_path:
            return Path(config_path)

        # Check FRAISIER_CONFIG environment variable
        env_path = os.environ.get("FRAISIER_CONFIG")
        if env_path:
            return Path(env_path)

        # Check standard locations (CWD first, then system-wide)
        locations = _config_search_locations()
        for loc in locations:
            if loc.exists():
                return loc

        locations_str = [str(p) for p in locations]
        raise FileNotFoundError(f"fraises.yaml not found in any of: {locations_str}")

    def _load(self) -> None:
        """Load configuration from YAML file."""
        with Path(self.config_path).open() as f:
            self._config = yaml.safe_load(f)
        self._validate_fraises()
        self._validate_branch_mapping()
        self._validate_notifications()
        self._validate_hooks()

    def _validate_fraises(self) -> None:
        """Validate all fraise configs at load time."""
        fraises = self._config.get("fraises", {})
        if not fraises:
            return
        for name, fraise in fraises.items():
            if not isinstance(fraise, dict):
                continue
            for env_config in fraise.get("environments", {}).values():
                if not isinstance(env_config, dict):
                    continue
                self._validate_environment(name, env_config)

    def _validate_environment(self, fraise_name: str, env: dict) -> None:
        """Validate a single fraise environment config."""
        errors: list[str] = []

        # app_path is required when health_check is configured (needs a deploy target)
        if env.get("health_check") and not env.get("app_path"):
            errors.append(f"{fraise_name}: 'app_path' is required")

        # Numeric fields in health_check
        hc = env.get("health_check", {})
        if isinstance(hc, dict):
            for field in ("timeout", "retries"):
                val = hc.get(field)
                if val is not None and not isinstance(val, int | float):
                    errors.append(
                        f"{fraise_name}: health_check.{field} must be a number, "
                        f"got {type(val).__name__}"
                    )

        # Numeric fields at top level
        for field in ("timeout", "lock_timeout"):
            val = env.get(field)
            if val is not None and not isinstance(val, int | float):
                errors.append(
                    f"{fraise_name}: '{field}' must be a number, "
                    f"got {type(val).__name__}"
                )

        # systemd_service name validation
        systemd_service = env.get("systemd_service")
        if systemd_service is not None:
            base = str(systemd_service)
            base = base.removesuffix(".service")
            if not base or not _UNIT_NAME_RE.match(base):
                errors.append(
                    f"{fraise_name}: systemd_service contains invalid characters: "
                    f"{systemd_service!r}"
                )

        # systemd_deploy_socket name validation
        systemd_deploy_socket = env.get("systemd_deploy_socket")
        if systemd_deploy_socket is not None:
            base = str(systemd_deploy_socket)
            base = base.removesuffix(".socket")
            if not base or not _UNIT_NAME_RE.match(base):
                errors.append(
                    f"{fraise_name}: systemd_deploy_socket contains invalid "
                    f"characters: {systemd_deploy_socket!r}"
                )

        # clone_url format validation
        clone_url = env.get("clone_url")
        if clone_url and not _GIT_URL_RE.match(str(clone_url)):
            errors.append(
                f"{fraise_name}: clone_url must be a valid git URL "
                f"(SSH, HTTPS, or absolute path), got: {clone_url!r}"
            )

        # Strategy validation
        db = env.get("database", {})
        if isinstance(db, dict):
            strategy = db.get("strategy")
            if strategy and strategy not in _VALID_STRATEGIES:
                valid = ", ".join(sorted(_VALID_STRATEGIES))
                errors.append(
                    f"{fraise_name}: unknown strategy '{strategy}'. Valid: {valid}"
                )
            if strategy == "restore_migrate":
                errors.extend(self._validate_restore_migrate(fraise_name, db))
            errors.extend(self._validate_database_url(fraise_name, db))

        if errors:
            raise ValidationError(
                f"Invalid fraise config: {'; '.join(errors)}",
            )

    @staticmethod
    def _validate_pg_url(fraise_name: str, field: str, value: Any) -> list[str]:
        """Return validation errors for a PostgreSQL URL field."""
        if value is None:
            return []
        if not isinstance(value, str):
            return [
                f"{fraise_name}: database.{field} must be a string, "
                f"got {type(value).__name__}"
            ]
        if not value.startswith(("postgresql://", "postgres://")):
            return [
                f"{fraise_name}: database.{field} must start with "
                f"'postgresql://' or 'postgres://'"
            ]
        return []

    @staticmethod
    def _validate_database_url(fraise_name: str, db: dict) -> list[str]:
        """Return validation errors for database_url and admin_url."""
        errors = FraisierConfig._validate_pg_url(
            fraise_name, "database_url", db.get("database_url")
        )
        errors.extend(
            FraisierConfig._validate_pg_url(
                fraise_name, "admin_url", db.get("admin_url")
            )
        )
        return errors

    @staticmethod
    def _validate_restore_migrate(fraise_name: str, db: dict) -> list[str]:
        """Return validation errors for a restore_migrate database config."""
        errors: list[str] = []
        restore = db.get("restore", {})
        if not isinstance(restore, dict) or not restore.get("backup_dir"):
            errors.append(
                f"{fraise_name}: strategy 'restore_migrate' requires "
                "database.restore.backup_dir"
            )
        if not db.get("name"):
            errors.append(
                f"{fraise_name}: strategy 'restore_migrate' requires database.name"
            )
        return errors

    _VALID_NOTIFIER_TYPES = frozenset(
        {
            "slack",
            "discord",
            "teams",
            "email",
            "webhook",
            "github_issue",
            "gitlab_issue",
            "gitea_issue",
            "bitbucket_issue",
        }
    )

    _REQUIRED_FIELDS: ClassVar[dict[str, list[str]]] = {
        "slack": ["webhook_url"],
        "discord": ["webhook_url"],
        "teams": ["webhook_url"],
        "email": ["from_email", "to_emails"],
        "webhook": ["url"],
        "github_issue": ["repo"],
        "gitlab_issue": ["repo"],
        "gitea_issue": ["repo"],
        "bitbucket_issue": ["repo"],
    }

    def _validate_branch_mapping(self) -> None:
        """Validate branch_mapping entries at load time."""
        raw = self._config.get("branch_mapping", {})
        if not raw:
            return

        fraises = self._config.get("fraises", {})

        for branch, mapping in raw.items():
            entries = [mapping] if isinstance(mapping, dict) else mapping
            if not isinstance(entries, list):
                continue

            seen: set[tuple[str, str]] = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                fraise_name = entry.get("fraise") or entry.get("fraise_name")
                environment = entry.get("environment")

                if not fraise_name:
                    raise ConfigurationError(
                        f"branch_mapping[{branch}]: entry missing 'fraise' key",
                    )
                if not environment:
                    raise ConfigurationError(
                        f"branch_mapping[{branch}]: entry missing 'environment' key",
                    )

                if fraise_name not in fraises:
                    raise ConfigurationError(
                        f"branch_mapping[{branch}]: fraise '{fraise_name}' "
                        f"not found in fraises config",
                    )

                fraise_cfg = fraises[fraise_name]
                envs = fraise_cfg.get("environments", {})
                if environment not in envs:
                    raise ConfigurationError(
                        f"branch_mapping[{branch}]: environment '{environment}' "
                        f"not found for fraise '{fraise_name}'",
                    )

                pair = (fraise_name, environment)
                if pair in seen:
                    raise ConfigurationError(
                        f"branch_mapping[{branch}]: duplicate "
                        f"({fraise_name}, {environment})",
                    )
                seen.add(pair)

    def _validate_notifications(self) -> None:
        """Validate the notifications: section."""
        notifications = self._config.get("notifications", {})
        if not notifications:
            return
        errors: list[str] = []
        for event_key in ("on_failure", "on_rollback", "on_success"):
            for notifier_cfg in notifications.get(event_key, []):
                if not isinstance(notifier_cfg, dict):
                    continue
                ntype = notifier_cfg.get("type", "")
                if ntype not in self._VALID_NOTIFIER_TYPES:
                    valid = ", ".join(sorted(self._VALID_NOTIFIER_TYPES))
                    errors.append(f"Unknown notifier type '{ntype}'. Valid: {valid}")
                    continue
                required = self._REQUIRED_FIELDS.get(ntype, [])
                errors.extend(
                    f"Notifier '{ntype}' missing required field '{req}'"
                    for req in required
                    if not notifier_cfg.get(req)
                )
        if errors:
            raise ValidationError(f"Invalid notification config: {'; '.join(errors)}")

    _VALID_HOOK_TYPES = frozenset({"backup", "audit"})

    _REQUIRED_HOOK_FIELDS: ClassVar[dict[str, list[str]]] = {
        "backup": ["backup_dir", "database_url"],
        "audit": ["database_path", "signing_key"],
    }

    _VALID_HOOK_PHASES = frozenset(
        {
            "before_deploy",
            "after_deploy",
            "before_rollback",
            "after_rollback",
            "on_failure",
        }
    )

    def _validate_hooks(self) -> None:
        """Validate the hooks: section."""
        hooks = self._config.get("hooks", {})
        if not hooks:
            return
        errors: list[str] = []
        for phase_key in hooks:
            if phase_key not in self._VALID_HOOK_PHASES:
                valid = ", ".join(sorted(self._VALID_HOOK_PHASES))
                errors.append(f"Unknown hook phase '{phase_key}'. Valid: {valid}")
                continue
            for hook_cfg in hooks.get(phase_key, []):
                if not isinstance(hook_cfg, dict):
                    continue
                htype = hook_cfg.get("type", "")
                if htype not in self._VALID_HOOK_TYPES:
                    valid = ", ".join(sorted(self._VALID_HOOK_TYPES))
                    errors.append(f"Unknown hook type '{htype}'. Valid: {valid}")
                    continue
                required = self._REQUIRED_HOOK_FIELDS.get(htype, [])
                errors.extend(
                    f"Hook '{htype}' missing required field '{req}'"
                    for req in required
                    if not hook_cfg.get(req)
                )
        if errors:
            raise ValidationError(f"Invalid hooks config: {'; '.join(errors)}")

    @property
    def notifications(self) -> dict[str, Any]:
        """Get notifications configuration."""
        return self._config.get("notifications", {})

    def reload(self) -> None:
        """Reload configuration from file."""
        self._load()

    @property
    def deployment(self) -> DeploymentConfig:
        """Get parsed deployment configuration with validation."""
        raw = self._config.get("deployment", {}) or {}
        strategies = raw.get("strategies", {}) or {}
        for env, strat in strategies.items():
            if strat not in _VALID_STRATEGIES:
                valid = ", ".join(sorted(_VALID_STRATEGIES))
                raise ValidationError(
                    f"Invalid strategy '{strat}' for {env}. Valid: {valid}",
                )
        lock_backend = raw.get("lock_backend", "file")
        if lock_backend not in _VALID_LOCK_BACKENDS:
            valid = ", ".join(sorted(_VALID_LOCK_BACKENDS))
            raise ValidationError(
                f"Invalid lock_backend '{lock_backend}'. Valid: {valid}",
            )

        return DeploymentConfig(
            lock_dir=raw.get("lock_dir", "/run/fraisier"),
            lock_backend=lock_backend,
            lock_db_path=raw.get("lock_db_path", "/var/lib/fraisier/locks.db"),
            status_file=raw.get("status_file", "deployment_status.json"),
            deploy_user=raw.get("deploy_user", "fraisier"),
            strategies=strategies,
            timeouts=raw.get("timeouts", {}) or {},
        )

    @property
    def scaffold(self) -> ScaffoldConfig:
        """Get parsed scaffold configuration with defaults."""
        raw = self._config.get("scaffold", {}) or {}
        raw_systemd = raw.get("systemd", {}) or {}
        raw_nginx = raw.get("nginx", {}) or {}
        raw_gh = raw.get("github_actions", {}) or {}
        raw_pg = raw.get("postgresql", {}) or {}

        # Fallback deploy_user: scaffold -> top-level -> deployment -> "fraisier"
        deploy_user = raw.get("deploy_user")
        if not deploy_user:
            deploy_user = self._config.get("deploy_user")
        if not deploy_user:
            dep_raw = self._config.get("deployment", {}) or {}
            deploy_user = dep_raw.get("deploy_user", "fraisier")

        return ScaffoldConfig(
            output_dir=raw.get("output_dir", "scripts/generated"),
            deploy_user=deploy_user,
            config_path=raw.get("config_path", "/opt/fraisier/fraises.yaml"),
            deploy_environment_file=raw.get("deploy_environment_file"),
            systemd=SystemdScaffoldConfig(
                security_hardening=raw_systemd.get("security_hardening", True),
                memory_max_default=raw_systemd.get("memory_max_default", "4G"),
            ),
            nginx=NginxScaffoldConfig(
                ssl_provider=raw_nginx.get("ssl_provider", "letsencrypt"),
                cors_origins=raw_nginx.get("cors_origins", []),
                rate_limit=raw_nginx.get("rate_limit", "10r/s"),
                restricted_paths=raw_nginx.get("restricted_paths", []),
                webhook_port=raw_nginx.get("webhook_port", 8080),
            ),
            github_actions=GithubActionsScaffoldConfig(
                python_versions=raw_gh.get("python_versions", ["3.12"]),
                test_command=raw_gh.get("test_command", "uv run pytest"),
                lint_command=raw_gh.get("lint_command", "uv run ruff check"),
                format_command=raw_gh.get(
                    "format_command", "uv run ruff format --check"
                ),
            ),
            postgresql=PostgresLoggingConfig(
                log_min_duration_statement=raw_pg.get("log_min_duration_statement"),
                log_statement=raw_pg.get("log_statement"),
                log_connections=raw_pg.get("log_connections"),
                log_line_prefix=raw_pg.get("log_line_prefix", "'%m [%p] %q%u@%d '"),
                log_min_error_statement=raw_pg.get("log_min_error_statement", "error"),
                log_error_verbosity=raw_pg.get("log_error_verbosity", "default"),
                deadlock_timeout=raw_pg.get("deadlock_timeout", "1s"),
                log_lock_waits=raw_pg.get("log_lock_waits", True),
                log_rotation_age=raw_pg.get("log_rotation_age", "1d"),
                log_rotation_size=raw_pg.get("log_rotation_size", "100MB"),
            ),
        )

    @property
    def health(self) -> HealthConfig:
        """Get parsed health configuration with defaults."""
        raw = self._config.get("health", {}) or {}
        raw_response = raw.get("response", {}) or {}
        return HealthConfig(
            startup_timeout_seconds=raw.get("startup_timeout_seconds", 120),
            deploy_poll_interval_seconds=raw.get("deploy_poll_interval_seconds", 5),
            endpoints=raw.get("endpoints", ["/health"]),
            response=HealthResponseConfig(
                include_version=raw_response.get("include_version", True),
                include_schema_hash=raw_response.get("include_schema_hash", True),
                include_response_time=raw_response.get("include_response_time", True),
                include_database=raw_response.get("include_database", False),
                include_environment=raw_response.get("include_environment", False),
                include_commit=raw_response.get("include_commit", False),
            ),
        )

    @property
    def ship(self) -> ShipConfig:
        """Get parsed ship pipeline configuration."""
        raw = self._config.get("ship", {}) or {}
        raw_checks = raw.get("checks", []) or []
        valid_phases = {"fix", "validate", "test"}
        checks = []
        for c in raw_checks:
            phase = c.get("phase", "validate")
            if phase not in valid_phases:
                raise ValidationError(
                    f"Invalid ship check phase '{phase}' for "
                    f"'{c.get('name', '?')}'. "
                    f"Valid: {', '.join(sorted(valid_phases))}",
                )
            checks.append(
                ShipCheckConfig(
                    name=c["name"],
                    command=c.get("command", []),
                    phase=phase,
                    triggers=c.get("triggers"),
                    timeout=c.get("timeout", 60),
                )
            )
        return ShipConfig(
            checks=checks,
            pr_base=raw.get("pr_base"),
            parallel=raw.get("parallel", True),
        )

    @property
    def project_name(self) -> str:
        """Project name used to prefix generated service names.

        Resolution order:
        1. Explicit ``name`` field in fraises.yaml
        2. Git repository basename
        3. Current working directory basename
        """
        name = self._config.get("name")
        if name:
            return str(name)

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            )
            return Path(result.stdout.strip()).name
        except (subprocess.CalledProcessError, FileNotFoundError):
            logging.getLogger(__name__).debug(
                "Could not determine git repo name, using cwd"
            )

        return Path.cwd().name

    @property
    def fraises(self) -> dict[str, dict[str, Any]]:
        """Get all fraise configurations."""
        return self._config.get("fraises", {})

    @property
    def environments(self) -> dict[str, dict[str, Any]]:
        """Get global environment configurations."""
        return self._config.get("environments", {})

    @property
    def branch_mapping(self) -> dict[str, list[dict[str, str]]]:
        """Get branch to fraise/environment mapping.

        Normalizes both single-dict and list-of-dicts syntax to always
        return lists, enabling monorepo workflows where one branch
        deploys multiple fraises.
        """
        raw = self._config.get("branch_mapping", {})
        result: dict[str, list[dict[str, str]]] = {}
        for branch, mapping in raw.items():
            if isinstance(mapping, dict):
                result[branch] = [mapping]
            elif isinstance(mapping, list):
                result[branch] = mapping
            else:
                result[branch] = []
        return result

    @property
    def webhook(self) -> dict[str, Any]:
        """Get global webhook configuration."""
        return self._config.get("webhook", {})

    def get_fraise(self, fraise_name: str) -> dict[str, Any] | None:
        """Get configuration for a fraise (all environments)."""
        return self.fraises.get(fraise_name)

    def get_fraise_environment(
        self, fraise_name: str, environment: str
    ) -> dict[str, Any] | None:
        """Get configuration for a specific fraise + environment.

        Args:
            fraise_name: e.g., "my_api", "etl", "backup"
            environment: e.g., "development", "staging", "production"

        Returns:
            Merged config with fraise-level and environment-level settings
        """
        fraise = self.fraises.get(fraise_name)
        if not fraise:
            return None

        env_config = fraise.get("environments", {}).get(environment)
        if not env_config:
            return None

        # Merge fraise-level config with environment-specific config
        merged = {
            "fraise_name": fraise_name,
            "environment": environment,
            "type": fraise.get("type"),
            "description": fraise.get("description"),
            **env_config,
        }
        # Inherit fraise-level install if environment doesn't override it
        if "install" not in merged and "install" in fraise:
            merged["install"] = fraise["install"]
        return merged

    def get_fraises_for_branch(self, branch: str) -> list[dict[str, Any]]:
        """Get fraise configurations for a git branch (webhook routing).

        Supports monorepo workflows where one branch maps to multiple fraises.

        Args:
            branch: Git branch name (e.g., "dev", "main")

        Returns:
            List of fraise+environment configs for the branch
        """
        mappings = self.branch_mapping.get(branch)
        if not mappings:
            return []

        results = []
        for mapping in mappings:
            fraise_name = mapping.get("fraise") or mapping.get("fraise_name")
            environment = mapping.get("environment")
            if not fraise_name or not environment:
                continue
            config = self.get_fraise_environment(fraise_name, environment)
            if config:
                results.append(config)
        return results

    def get_fraise_for_branch(self, branch: str) -> dict[str, Any] | None:
        """Get fraise configuration for a git branch (webhook routing).

        .. deprecated::
            Use :meth:`get_fraises_for_branch` for multi-fraise support.

        Returns:
            Full fraise+environment config for the first mapped fraise
        """
        results = self.get_fraises_for_branch(branch)
        return results[0] if results else None

    def list_fraises(self) -> list[str]:
        """List all fraise names.

        Returns:
            List of fraise name strings
        """
        return list(self.fraises.keys())

    def list_all_deployments(self) -> list[dict[str, Any]]:
        """List all fraise+environment combinations (deployable targets).

        Returns:
            List of all deployable targets
        """
        result = []
        for fraise_name, fraise in self.fraises.items():
            fraise_type = fraise.get("type", "unknown")
            description = fraise.get("description", "")

            for env_name, env_config in fraise.get("environments", {}).items():
                # Handle fraises with nested jobs (backup, statistics)
                if "jobs" in env_config:
                    for job_name, job_config in env_config["jobs"].items():
                        result.append(
                            {
                                "fraise": fraise_name,
                                "environment": env_name,
                                "job": job_name,
                                "type": fraise_type,
                                "name": job_config.get("name", job_name),
                                "description": job_config.get(
                                    "description", description
                                ),
                            }
                        )
                else:
                    result.append(
                        {
                            "fraise": fraise_name,
                            "environment": env_name,
                            "job": None,
                            "type": fraise_type,
                            "name": env_config.get("name", fraise_name),
                            "description": description,
                        }
                    )
        return result

    def get_deployments_by_type(self, fraise_type: str) -> list[dict[str, Any]]:
        """Get all deployments of a specific type."""
        return [d for d in self.list_all_deployments() if d["type"] == fraise_type]

    def get_deployments_by_environment(self, environment: str) -> list[dict[str, Any]]:
        """Get all deployments for a specific environment."""
        return [
            d for d in self.list_all_deployments() if d["environment"] == environment
        ]

    def get_environment(
        self, fraise_name: str, environment: str
    ) -> dict[str, Any] | None:
        """Get environment config for a fraise. Alias for get_fraise_environment."""
        return self.get_fraise_environment(fraise_name, environment)

    def get_deploy_user(self, fraise_name: str, environment: str) -> str:
        """Resolve effective deploy_user for a fraise/environment pair.

        Priority: environment-level deploy_user > scaffold.deploy_user.
        """
        env = self.get_fraise_environment(fraise_name, environment)
        if env and env.get("deploy_user"):
            return env["deploy_user"]
        return self.scaffold.deploy_user

    def get_git_provider_config(self) -> dict[str, Any]:
        """Get git provider configuration."""
        return self._config.get("git", {})

    def list_environments(self, fraise_name: str) -> list[str]:
        """List environment names for a fraise."""
        fraise = self.fraises.get(fraise_name)
        if not fraise:
            return []
        return list(fraise.get("environments", {}).keys())

    def get_environments_for_server(self, server: str) -> list[str]:
        """Return environment names whose ``server`` field matches *server*.

        Checks both the global ``environments`` section and per-fraise
        environment configs, deduplicating the result.
        Returns an empty list when no environment declares that server.
        """
        matched: dict[str, None] = {}

        # Check global environments section
        for env_name, env_config in self.environments.items():
            if env_config.get("server") == server:
                matched[env_name] = None

        # Check per-fraise environment configs
        for fraise in self.fraises.values():
            for env_name, env_config in fraise.get("environments", {}).items():
                if env_config.get("server") == server:
                    matched[env_name] = None

        return list(matched)

    def list_fraises_detailed(self) -> list[dict[str, Any]]:
        """List all fraises with detailed info (type, description, environments)."""
        result = []
        for fraise_name, fraise in self.fraises.items():
            environments = list(fraise.get("environments", {}).keys())
            result.append(
                {
                    "name": fraise_name,
                    "type": fraise.get("type", "unknown"),
                    "description": fraise.get("description", ""),
                    "environments": environments,
                }
            )
        return result


# Global config instance (lazy loaded, thread-safe)
_config: FraisierConfig | None = None
_config_lock = threading.Lock()


def get_config(config_path: Path | str | None = None) -> FraisierConfig:
    """Get or create global configuration instance."""
    global _config
    if _config is None or config_path:
        with _config_lock:
            if _config is None or config_path:
                _config = FraisierConfig(config_path)
    return _config


def reset_config() -> None:
    """Reset the global configuration singleton.

    Next call to ``get_config()`` will re-read from disk.
    """
    global _config
    with _config_lock:
        _config = None

"""Configuration validation for fraises.yaml.

Free functions that validate sections of the raw config dict loaded by
:class:`fraisier.config.loader.FraisierConfig`. They raise
:class:`fraisier.errors.ValidationError` or
:class:`fraisier.errors.ConfigurationError` on invalid input.
"""

import re
from typing import Any

from fraisier.config.schema import _UNIT_NAME_RE, _VALID_STRATEGIES
from fraisier.dbops._strategies import ADMIN_STRATEGIES
from fraisier.errors import ConfigurationError, ValidationError

_GIT_URL_RE = re.compile(
    r"^("
    r"https?://[^\s]+"
    r"|git@[\w.\-]+:[\w./-]+"
    r"|ssh://[^\s]+"
    r"|/[\w./-]+"
    r")$"
)

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

_REQUIRED_FIELDS: dict[str, list[str]] = {
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

_VALID_HOOK_TYPES = frozenset({"backup", "audit"})

_REQUIRED_HOOK_FIELDS: dict[str, list[str]] = {
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


def validate_fraises(fraises: dict) -> None:
    """Validate all fraise configs at load time."""
    if not fraises:
        return
    for name, fraise in fraises.items():
        if not isinstance(fraise, dict):
            continue
        for env_config in fraise.get("environments", {}).values():
            if not isinstance(env_config, dict):
                continue
            _validate_environment(name, env_config)


def validate_servers(servers: dict) -> None:
    """Validate servers section: no machine hostname should appear twice."""
    if not servers:
        return

    machines_to_servers: dict[str, str] = {}
    for logical_server, server_config in servers.items():
        if not isinstance(server_config, dict):
            continue
        machine_list = server_config.get("machine_hostnames", []) or []
        for machine in machine_list:
            if machine in machines_to_servers:
                existing = machines_to_servers[machine]
                raise ValidationError(
                    f"Machine '{machine}' appears under both "
                    f"'{existing}' and '{logical_server}' in servers:. "
                    f"Each machine can only belong to one logical server."
                )
            machines_to_servers[machine] = logical_server


def _validate_environment(fraise_name: str, env: dict) -> None:
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

        # Headers should be a dict
        headers = hc.get("headers")
        if headers is not None and not isinstance(headers, dict):
            errors.append(
                f"{fraise_name}: health_check.headers must be a dict, "
                f"got {type(headers).__name__}"
            )

        # Field mappings should be strings
        for field in ("version_field", "migration_field"):
            val = hc.get(field)
            if val is not None and not isinstance(val, str):
                errors.append(
                    f"{fraise_name}: health_check.{field} must be a string, "
                    f"got {type(val).__name__}"
                )

    # Numeric fields at top level
    for field in ("timeout", "lock_timeout"):
        val = env.get(field)
        if val is not None and not isinstance(val, int | float):
            errors.append(
                f"{fraise_name}: '{field}' must be a number, got {type(val).__name__}"
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

    # ssh: block validation
    ssh = env.get("ssh")
    if ssh is not None:
        if not isinstance(ssh, dict):
            errors.append(
                f"{fraise_name}: 'ssh' must be a mapping, got {type(ssh).__name__}"
            )
        else:
            if not ssh.get("host"):
                errors.append(f"{fraise_name}: ssh.host is required")
            for str_field in ("host", "user", "key_path"):
                val = ssh.get(str_field)
                if val is not None and not isinstance(val, str):
                    errors.append(
                        f"{fraise_name}: ssh.{str_field} must be a string, "
                        f"got {type(val).__name__}"
                    )
            port = ssh.get("port")
            if port is not None and not isinstance(port, int):
                errors.append(
                    f"{fraise_name}: ssh.port must be an integer, "
                    f"got {type(port).__name__}"
                )
            strict = ssh.get("strict_host_key")
            if strict is not None and not isinstance(strict, bool):
                errors.append(
                    f"{fraise_name}: ssh.strict_host_key must be a boolean, "
                    f"got {type(strict).__name__}"
                )
            connect_timeout = ssh.get("connect_timeout")
            if connect_timeout is not None and not isinstance(connect_timeout, int):
                errors.append(
                    f"{fraise_name}: ssh.connect_timeout must be an integer, "
                    f"got {type(connect_timeout).__name__}"
                )
            address_family = ssh.get("address_family")
            if address_family is not None and address_family not in (
                "inet",
                "inet6",
                "any",
            ):
                errors.append(
                    f"{fraise_name}: ssh.address_family must be 'inet', 'inet6', "
                    f"or 'any', got {address_family!r}"
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
            errors.extend(_validate_restore_migrate(fraise_name, db))
        if strategy in ADMIN_STRATEGIES and not db.get("admin_url"):
            errors.append(
                f"{fraise_name}: strategy '{strategy}' requires "
                "database.admin_url. Fix: add admin_url, e.g. "
                "postgresql:///postgres?host=/var/run/postgresql"
            )
        errors.extend(_validate_database_url(fraise_name, db))

    # ZFS configuration validation
    zfs_config = env.get("zfs")
    if zfs_config is not None:
        errors.extend(_validate_zfs_config(fraise_name, zfs_config))

    if errors:
        raise ValidationError(
            f"Invalid fraise config: {'; '.join(errors)}",
        )


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
            f"{fraise_name}: database.{field} must be a PostgreSQL URL "
            f"(starting with postgresql:// or postgres://), got: {value!r}"
        ]
    return []


def _validate_zfs_config(fraise_name: str, zfs: Any) -> list[str]:
    """Validate ZFS configuration section."""
    errors: list[str] = []

    if not isinstance(zfs, dict):
        errors.append(
            f"{fraise_name}: 'zfs' must be a mapping, got {type(zfs).__name__}"
        )
        return errors

    # Check enabled flag
    enabled = zfs.get("enabled", False)
    if not isinstance(enabled, bool):
        got = type(enabled).__name__
        errors.append(f"{fraise_name}: zfs.enabled must be a boolean, got {got}")

    if enabled:
        # Required fields when enabled
        for field in ["pool", "data_dataset"]:
            value = zfs.get(field)
            if not value:
                errors.append(
                    f"{fraise_name}: zfs.{field} is required when ZFS is enabled"
                )
            elif not isinstance(value, str):
                got = type(value).__name__
                errors.append(f"{fraise_name}: zfs.{field} must be a string, got {got}")

        # Optional string fields
        for field in ["snapshot_prefix", "clone_prefix"]:
            value = zfs.get(field)
            if value is not None:
                if not isinstance(value, str):
                    got = type(value).__name__
                    errors.append(
                        f"{fraise_name}: zfs.{field} must be a string, got {got}"
                    )
                elif not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", value):
                    errors.append(
                        f"{fraise_name}: zfs.{field} '{value}'"
                        " must contain only alphanumeric characters"
                        " and underscores, and start with a letter"
                        " or underscore"
                    )

        # Optional numeric fields
        for field in ["max_snapshot_age_days", "snapshot_retention"]:
            value = zfs.get(field)
            if value is not None:
                if not isinstance(value, int):
                    got = type(value).__name__
                    errors.append(
                        f"{fraise_name}: zfs.{field} must be an integer, got {got}"
                    )
                elif value <= 0:
                    errors.append(f"{fraise_name}: zfs.{field} must be positive")

    return errors


def _validate_database_url(fraise_name: str, db: dict) -> list[str]:
    """Return validation errors for database_url and admin_url."""
    errors = _validate_pg_url(fraise_name, "database_url", db.get("database_url"))
    errors.extend(_validate_pg_url(fraise_name, "admin_url", db.get("admin_url")))
    return errors


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


def validate_branch_mapping(branch_mapping: dict, fraises: dict) -> None:
    """Validate branch_mapping entries at load time."""
    if not branch_mapping:
        return

    for branch, mapping in branch_mapping.items():
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


def validate_service_manager(service_manager: str | None) -> None:
    """Validate service_manager field."""
    if service_manager is None:
        return
    valid_values = {"systemd", "rc"}
    if service_manager not in valid_values:
        raise ValidationError(
            f"Invalid service_manager: {service_manager!r}. "
            f"Must be one of: {', '.join(sorted(valid_values))}"
        )


def validate_notifications(notifications: dict) -> None:
    """Validate the notifications: section."""
    if not notifications:
        return
    errors: list[str] = []
    for event_key in ("on_failure", "on_rollback", "on_success"):
        for notifier_cfg in notifications.get(event_key, []):
            if not isinstance(notifier_cfg, dict):
                continue
            ntype = notifier_cfg.get("type", "")
            if ntype not in _VALID_NOTIFIER_TYPES:
                valid = ", ".join(sorted(_VALID_NOTIFIER_TYPES))
                errors.append(f"Unknown notifier type '{ntype}'. Valid: {valid}")
                continue
            required = _REQUIRED_FIELDS.get(ntype, [])
            errors.extend(
                f"Notifier '{ntype}' missing required field '{req}'"
                for req in required
                if not notifier_cfg.get(req)
            )
    if errors:
        raise ValidationError(f"Invalid notification config: {'; '.join(errors)}")


def validate_hooks(hooks: dict) -> None:
    """Validate the hooks: section."""
    if not hooks:
        return
    errors: list[str] = []
    for phase_key in hooks:
        if phase_key not in _VALID_HOOK_PHASES:
            valid = ", ".join(sorted(_VALID_HOOK_PHASES))
            errors.append(f"Unknown hook phase '{phase_key}'. Valid: {valid}")
            continue
        for hook_cfg in hooks.get(phase_key, []):
            if not isinstance(hook_cfg, dict):
                continue
            htype = hook_cfg.get("type", "")
            if htype not in _VALID_HOOK_TYPES:
                valid = ", ".join(sorted(_VALID_HOOK_TYPES))
                errors.append(f"Unknown hook type '{htype}'. Valid: {valid}")
                continue
            required = _REQUIRED_HOOK_FIELDS.get(htype, [])
            errors.extend(
                f"Hook '{htype}' missing required field '{req}'"
                for req in required
                if not hook_cfg.get(req)
            )
    if errors:
        raise ValidationError(f"Invalid hooks config: {'; '.join(errors)}")

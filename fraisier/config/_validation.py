"""Configuration validation for fraises.yaml.

Free functions that validate sections of the raw config dict loaded by
:class:`fraisier.config.loader.FraisierConfig`. They raise
:class:`fraisier.errors.ValidationError` or
:class:`fraisier.errors.ConfigurationError` on invalid input.
"""

import re
from typing import Any, cast

from fraisier.config._lazy_env import LazyEnv, is_string_like
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
        validate_one_fraise(name, fraise)


def validate_one_fraise(name: str, fraise: Any) -> None:
    """Validate a single fraise (all of its environments).

    Used both by :func:`validate_fraises` (bulk path, e.g. ``fraisier
    validate``'s full traversal) and by the lazy per-section validator
    behind :meth:`FraisierConfig.get_fraise_environment`.
    """
    if not isinstance(fraise, dict):
        return
    for env_config in fraise.get("environments", {}).values():
        if not isinstance(env_config, dict):
            continue
        _validate_environment(name, env_config)


def validate_one_fraise_environment(name: str, env_name: str, env_config: Any) -> None:
    """Validate a single fraise environment.

    Single point of entry for Stage-2 per-env validation behind
    ``FraisierConfig._get_validated_env``. ``env_name`` is unused by the
    underlying validator today but accepted so the call signature carries
    the natural ``(fraise, environment)`` key.
    """
    del env_name  # currently unused — kept for call-site readability
    if not isinstance(env_config, dict):
        return
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

    # systemd_service name validation — defers content check on LazyEnv;
    # consumers re-check after to_str() at unit-file emission time.
    systemd_service = env.get("systemd_service")
    if systemd_service is not None and not isinstance(systemd_service, LazyEnv):
        base = str(systemd_service)
        base = base.removesuffix(".service")
        if not base or not _UNIT_NAME_RE.match(base):
            errors.append(
                f"{fraise_name}: systemd_service contains invalid characters: "
                f"{systemd_service!r}"
            )

    # systemd_deploy_socket name validation — same LazyEnv deferral.
    systemd_deploy_socket = env.get("systemd_deploy_socket")
    if systemd_deploy_socket is not None and not isinstance(
        systemd_deploy_socket, LazyEnv
    ):
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
                if val is not None and not is_string_like(val):
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

    # clone_url format validation — deferred for LazyEnv.
    clone_url = env.get("clone_url")
    if (
        clone_url
        and not isinstance(clone_url, LazyEnv)
        and not _GIT_URL_RE.match(str(clone_url))
    ):
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

    # Preflight configuration validation
    if isinstance(db, dict) and db.get("preflight"):
        errors.extend(_validate_preflight(fraise_name, db))

    # post_migrate hook validation (#204)
    if isinstance(db, dict) and db.get("post_migrate") is not None:
        errors.extend(_validate_post_migrate(fraise_name, db))

    # smoke_tests validation (#204 PR B)
    if env.get("smoke_tests") is not None:
        errors.extend(_validate_smoke_tests(fraise_name, env))

    # ZFS configuration validation
    zfs_config = env.get("zfs")
    if zfs_config is not None:
        errors.extend(_validate_zfs_config(fraise_name, zfs_config))

    if errors:
        raise ValidationError(
            f"Invalid fraise config: {'; '.join(errors)}",
        )


def _validate_pg_url(fraise_name: str, field: str, value: Any) -> list[str]:
    """Return validation errors for a PostgreSQL URL field.

    ``LazyEnv`` values are accepted without inspecting the URL scheme
    — the scheme check is deferred to consumers, which call
    :func:`validate_pg_url_string` on the resolved value at use time.
    """
    if value is None:
        return []
    if isinstance(value, LazyEnv):
        return []
    if not isinstance(value, str):
        return [
            f"{fraise_name}: database.{field} must be a string, "
            f"got {type(value).__name__}"
        ]
    return validate_pg_url_string(fraise_name, field, value)


def validate_pg_url_string(fraise_name: str, field: str, value: str) -> list[str]:
    """Return scheme-check errors for a resolved PostgreSQL URL string.

    Consumer-side helper: call after :func:`fraisier.config.to_str` if the
    YAML field was sourced from ``!envvar`` and the URL scheme needs
    enforcement (database connection time).
    """
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
        # Required fields when enabled. LazyEnv is truthy and string-like,
        # so the "missing/empty" + type checks pass; the content shape
        # check is deferred (no regex on a deferred value).
        for field in ["pool", "data_dataset"]:
            value = zfs.get(field)
            if not value:
                errors.append(
                    f"{fraise_name}: zfs.{field} is required when ZFS is enabled"
                )
            elif not is_string_like(value):
                got = type(value).__name__
                errors.append(f"{fraise_name}: zfs.{field} must be a string, got {got}")

        # Optional string fields — alphanumeric regex check defers for
        # LazyEnv. Consumers re-validate the resolved value.
        for field in ["snapshot_prefix", "clone_prefix"]:
            value = zfs.get(field)
            if value is not None:
                if not is_string_like(value):
                    got = type(value).__name__
                    errors.append(
                        f"{fraise_name}: zfs.{field} must be a string, got {got}"
                    )
                elif isinstance(value, str) and not re.match(
                    r"^[a-zA-Z_][a-zA-Z0-9_]*$", value
                ):
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
    if isinstance(restore, dict):
        jobs = restore.get("jobs")
        if jobs is not None:
            if not isinstance(jobs, int) or isinstance(jobs, bool) or jobs < 1:
                errors.append(
                    f"{fraise_name}: restore.jobs must be a positive integer, "
                    f"got {jobs!r}"
                )
        pref = restore.get("preferred_compression")
        if pref is not None and not isinstance(pref, LazyEnv):
            valid_algos = {"zstd", "lz4", "gzip", "none"}
            if not isinstance(pref, str) or pref not in valid_algos:
                errors.append(
                    f"{fraise_name}: restore.preferred_compression must be one "
                    f"of {', '.join(sorted(valid_algos))}, got {pref!r}"
                )
    return errors


def _validate_preflight(fraise_name: str, db: dict) -> list[str]:
    """Return validation errors for a database.preflight config block."""
    errors: list[str] = []
    preflight = db.get("preflight", {})
    if not isinstance(preflight, dict):
        return errors

    enabled = preflight.get("enabled", True)
    strategy = db.get("strategy")

    if enabled and strategy != "restore_migrate":
        errors.append(
            f"{fraise_name}: database.preflight.enabled requires "
            f"strategy 'restore_migrate' (got {strategy!r}). "
            "Preflight only applies to the restore_migrate strategy."
        )

    timeout = preflight.get("timeout_seconds")
    if timeout is not None and timeout <= 0:
        errors.append(
            f"{fraise_name}: database.preflight.timeout_seconds must be "
            f"a positive integer, got {timeout!r}"
        )

    return errors


_VALID_POST_MIGRATE_ON_ERROR = frozenset({"halt", "warn"})


def _validate_smoke_tests(fraise_name: str, env: dict) -> list[str]:
    """Run the smoke_tests loader at config-load time to surface errors.

    Defers to ``fraisier.smoke_tests.load_smoke_tests`` so the schema
    (method, on_failure, JSONPath syntax) is enforced in one place. The
    base_url is derived from the env's ``health_check.url`` if any.
    """
    from urllib.parse import urlparse

    from fraisier.errors import ConfigurationError
    from fraisier.smoke_tests import load_smoke_tests

    hc = env.get("health_check") or {}
    hc_url = hc.get("url") if isinstance(hc, dict) else None
    base_url: str | None = None
    if hc_url:
        parsed = urlparse(str(hc_url))
        if parsed.scheme and parsed.netloc:
            base_url = f"{parsed.scheme}://{parsed.netloc}"
    try:
        load_smoke_tests(env, base_url=base_url)
    except (ValueError, ConfigurationError) as exc:
        return [f"{fraise_name}: smoke_tests {exc}"]
    return []


def _validate_post_migrate(fraise_name: str, db: dict) -> list[str]:
    """Return validation errors for a database.post_migrate config block."""
    errors: list[str] = []
    entries: Any = db.get("post_migrate")
    if not isinstance(entries, list):
        errors.append(
            f"{fraise_name}: database.post_migrate must be a list, "
            f"got {type(entries).__name__}"
        )
        return errors

    for index, raw_entry in enumerate(entries):
        location = f"database.post_migrate[{index}]"
        if not isinstance(raw_entry, dict):
            errors.append(
                f"{fraise_name}: {location} must be a mapping, "
                f"got {type(raw_entry).__name__}"
            )
            continue

        entry = cast("dict[str, Any]", raw_entry)
        sql_dir = entry.get("sql_dir")
        sql_file = entry.get("sql_file")
        if sql_dir and sql_file:
            errors.append(
                f"{fraise_name}: {location} must specify either sql_dir "
                "or sql_file, not both"
            )
        elif not sql_dir and not sql_file:
            errors.append(f"{fraise_name}: {location} must specify sql_dir or sql_file")

        on_error = entry.get("on_error", "halt")
        if on_error not in _VALID_POST_MIGRATE_ON_ERROR:
            errors.append(
                f"{fraise_name}: {location}.on_error must be 'halt' or "
                f"'warn', got {on_error!r}"
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

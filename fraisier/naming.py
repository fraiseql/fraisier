"""Centralised naming helpers for systemd unit names.

Resolution of ``!envvar``-tagged ``systemd_service`` /
``systemd_deploy_socket`` fields happens here, at the read boundary.
Downstream consumers (validation, remote_validator, setup, cli
helpers, cli/db, cli/_diagnose) call :func:`resolve_systemd_service`
and :func:`resolve_systemd_deploy_socket` to get a concrete
``str | None`` — never a ``LazyEnv`` — before shelling out to
``systemctl`` (which won't autocoerce) or computing string-method
names like ``.removesuffix(".service")``.
"""

from __future__ import annotations

from pathlib import Path

from fraisier.config._lazy_env import LazyEnv, to_str


def resolve_systemd_service(env_config: dict) -> str | None:
    """Return ``env_config['systemd_service']`` resolved to a ``str | None``.

    A ``LazyEnv`` value is materialized at this boundary, surfacing
    unset-env-var errors with their YAML path. ``None`` and missing
    keys both return ``None`` so callers can use a single truthy check.
    """
    value = env_config.get("systemd_service")
    if value is None:
        return None
    if isinstance(value, LazyEnv):
        return to_str(value)
    return str(value)


def resolve_systemd_deploy_socket(env_config: dict) -> str | None:
    """Return ``env_config['systemd_deploy_socket']`` resolved to a ``str | None``.

    See :func:`resolve_systemd_service` for the contract.
    """
    value = env_config.get("systemd_deploy_socket")
    if value is None:
        return None
    if isinstance(value, LazyEnv):
        return to_str(value)
    return str(value)


def deploy_socket_name(
    env_config: dict, env_key: str = "", fraise_name: str = ""
) -> str:
    """Return the systemd deploy socket unit name for an environment.

    Resolution order:
    1. env_config["systemd_deploy_socket"] (explicit override)
    2. f"fraisier-{env_config['name']}.socket" (derived from name field)
    3. f"fraisier-{fraise_name}-{env_key}.socket" (fraise + env key, unique per fraise)
    4. f"fraisier-{env_key}.socket" (env key only, legacy fallback)
    """
    if override := resolve_systemd_deploy_socket(env_config):
        return override if override.endswith(".socket") else f"{override}.socket"
    if name := env_config.get("name"):
        return f"fraisier-{name}.socket"
    if fraise_name:
        return f"fraisier-{fraise_name}-{env_key}.socket"
    return f"fraisier-{env_key}.socket"


def unit_installer_unit_names(project_name: str, env_name: str) -> tuple[str, str]:
    """Return ``(socket_unit, service_unit)`` for the unit-installer helper.

    One helper per (project, environment) (#240). The renderer names the files
    it writes and the artifact manifest names the files it installs; deriving
    that name in both places independently is the drift this module exists to
    prevent, so both call here.

    Returns:
        The ``.socket`` and ``.service`` unit names, in the order they are
        installed.
    """
    base = f"fraisier-{project_name}-{env_name}-unit-installer"
    return f"{base}.socket", f"{base}.service"


def unit_installer_socket_path(project_name: str, env_name: str) -> Path:
    """Return the filesystem path the unit-installer helper listens on.

    The socket unit's ``ListenStream=`` decides where the socket *is*;
    ``cli/scheduled_install`` and ``deployers/scheduled`` decide where to
    look for it. All three read this (#337). Both consumers degrade
    rather than fail when the socket is absent, so drift between them
    surfaces as auto-install quietly not happening — never as a crash,
    which is exactly why it needs an authority rather than vigilance.
    """
    return Path(f"/run/fraisier/{env_name}/unit-installer-{project_name}.sock")


def app_service_name(
    project_name: str,
    fraise_name: str,
    env_name: str,
    env_config: dict,
) -> str:
    """Return the systemd app service unit name (with .service suffix).

    Resolution order:
    1. ``systemd_service`` at the environment top level
    2. ``service.service_name`` (nested under the service: key)
    3. Default: ``{project_name}_{fraise_name}_{env_name}.service``
    """
    systemd_service = resolve_systemd_service(env_config)
    if systemd_service:
        base = systemd_service.removesuffix(".service")
        return f"{base}.service"

    override = (env_config.get("service") or {}).get("service_name")
    if override:
        return f"{override}.service"

    return f"{project_name}_{fraise_name}_{env_name}.service"

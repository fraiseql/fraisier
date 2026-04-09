"""Centralised naming helpers for systemd unit names."""

from __future__ import annotations


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
    if override := env_config.get("systemd_deploy_socket"):
        return override if override.endswith(".socket") else f"{override}.socket"
    if name := env_config.get("name"):
        return f"fraisier-{name}.socket"
    if fraise_name:
        return f"fraisier-{fraise_name}-{env_key}.socket"
    return f"fraisier-{env_key}.socket"

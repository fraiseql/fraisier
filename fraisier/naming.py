"""Centralised naming helpers for systemd unit names."""

from __future__ import annotations


def deploy_socket_name(env_config: dict, env_key: str = "") -> str:
    """Return the systemd deploy socket unit name for an environment.

    Resolution order:
    1. env_config["systemd_deploy_socket"] (explicit override)
    2. f"fraisier-{env_config['name']}.socket" (derived from name field)
    3. f"fraisier-{env_key}.socket" (derived from environment key)
    """
    if override := env_config.get("systemd_deploy_socket"):
        return override if override.endswith(".socket") else f"{override}.socket"
    name = env_config.get("name") or env_key
    return f"fraisier-{name}.socket"

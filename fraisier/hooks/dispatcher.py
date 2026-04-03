"""Build and register lifecycle hooks from fraises.yaml configuration."""

from __future__ import annotations

import os
from typing import Any

from fraisier.hooks.base import HookPhase, HookRunner


def _expand_env(value: str) -> str:
    """Expand ${VAR} references in a string."""
    if "${" not in value:
        return value
    return os.path.expandvars(value)


def build_hook_runner(config: dict[str, Any]) -> HookRunner:
    """Build a HookRunner from the ``hooks:`` section of fraises.yaml.

    Example config::

        hooks:
          before_deploy:
            - type: backup
              backup_dir: /var/lib/fraisier/backups
              database_url: ${DATABASE_URL}
          after_deploy:
            - type: audit
              database_path: /var/lib/fraisier/audit.db
              signing_key: ${AUDIT_KEY}
          on_failure:
            - type: audit
              database_path: /var/lib/fraisier/audit.db
              signing_key: ${AUDIT_KEY}
    """
    runner = HookRunner()
    hooks_config = config.get("hooks", {})
    if not hooks_config:
        return runner

    phase_map = {
        "before_deploy": HookPhase.BEFORE_DEPLOY,
        "after_deploy": HookPhase.AFTER_DEPLOY,
        "before_rollback": HookPhase.BEFORE_ROLLBACK,
        "after_rollback": HookPhase.AFTER_ROLLBACK,
        "on_failure": HookPhase.ON_FAILURE,
    }

    for key, phase in phase_map.items():
        for hook_cfg in hooks_config.get(key, []):
            hook = _build_hook(hook_cfg)
            runner.register(phase, hook)

    return runner


def _build_hook(cfg: dict[str, Any]) -> Any:
    """Build a single Hook from a config dict."""
    htype = cfg["type"]

    if htype == "backup":
        from fraisier.hooks.backup import BackupHook

        return BackupHook(
            backup_dir=_expand_env(cfg["backup_dir"]),
            database_url=_expand_env(cfg["database_url"]),
            compress=cfg.get("compress", True),
            max_backups=cfg.get("max_backups", 10),
        )

    if htype == "audit":
        from fraisier.hooks.audit import AuditHook

        return AuditHook(
            database_path=_expand_env(cfg["database_path"]),
            signing_key=_expand_env(cfg["signing_key"]),
        )

    msg = f"Unknown hook type: {htype!r}"
    raise ValueError(msg)

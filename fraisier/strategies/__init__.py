"""Deployment strategies — what to do with the database at each stage.

Three deployment strategies:
- **migrate**: preflight → migrate up.  Rollback via migrate down.  (production)
- **rebuild**: drop + rebuild from DDL.  (development)
- **restore_migrate**: restore backup → migrate up.  Rollback via down.  (staging)

Plus migration framework adapters for Django, Alembic, Peewee, and Confiture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._alembic import AlembicMigrateStrategy
from ._base import (
    MigrationResult,
    MigrationStrategy,
    Strategy,
    StrategyResult,
    ValidationResult,
)
from ._confiture import ConfitureMigrateStrategy
from ._core import MigrateStrategy, RebuildStrategy
from ._django import DjangoMigrateStrategy
from ._peewee import PeeweeMigrateStrategy
from ._restore import RestoreConfig, RestoreMigrateStrategy

__all__ = [
    "AlembicMigrateStrategy",
    "ConfitureMigrateStrategy",
    "DjangoMigrateStrategy",
    "MigrateStrategy",
    "MigrationResult",
    "MigrationStrategy",
    "PeeweeMigrateStrategy",
    "RebuildStrategy",
    "RestoreConfig",
    "RestoreMigrateStrategy",
    "Strategy",
    "StrategyResult",
    "ValidationResult",
    "get_strategy",
]


def get_strategy(name: str, **kwargs: Any) -> Strategy:
    """Factory for deployment strategies.

    Args:
        name: Strategy name (migrate, rebuild, restore_migrate).
        **kwargs: Extra args.  For ``restore_migrate``:
            ``db_name`` (str) and ``restore_config`` (dict from YAML).
    """
    if name == "migrate":
        return MigrateStrategy()
    if name == "rebuild":
        roles = kwargs.get("required_roles") or []
        project_dir = kwargs.get("project_dir")
        admin_url = kwargs.get("admin_url")
        return RebuildStrategy(
            required_roles=roles, project_dir=project_dir, admin_url=admin_url
        )
    if name == "restore_migrate":
        restore_cfg = kwargs.get("restore_config")
        if not restore_cfg or not isinstance(restore_cfg, dict):
            raise ValueError("restore_migrate strategy requires restore_config dict")
        db_name = kwargs.get("db_name", "")
        if not db_name:
            raise ValueError("restore_migrate strategy requires db_name")
        admin_url = kwargs.get("admin_url")
        if not admin_url:
            raise ValueError("restore_migrate strategy requires admin_url")
        backup_path = Path(kwargs["backup_path"]) if kwargs.get("backup_path") else None
        config = RestoreConfig(
            db_name=db_name,
            backup_dir=Path(restore_cfg["backup_dir"]),
            backup_pattern=restore_cfg.get("backup_pattern", "*.dump"),
            max_age_hours=float(restore_cfg.get("max_age_hours", 48.0)),
            target_owner=restore_cfg.get("target_owner"),
            create_template=bool(restore_cfg.get("create_template", False)),
            template_name=restore_cfg.get("template_name"),
            min_tables=int(restore_cfg.get("min_tables", 0)),
            jobs=int(restore_cfg.get("jobs", 1)),
            backup_path=backup_path,
        )
        service_manager = kwargs.get("service_manager")
        service_name = kwargs.get("service_name")
        return RestoreMigrateStrategy(
            config,
            admin_url=admin_url,
            service_manager=service_manager,
            service_name=service_name,
        )
    valid = "migrate, rebuild, restore_migrate"
    raise ValueError(f"Unknown strategy '{name}'. Valid: {valid}")

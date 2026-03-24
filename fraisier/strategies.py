"""Deployment strategies — what to do with the database at each stage.

Three strategies:
- **migrate**: preflight → migrate up.  Rollback via migrate down.  (production)
- **rebuild**: drop + rebuild from DDL.  (development)
- **restore_migrate**: restore backup → migrate up.  Rollback via down.  (staging)
"""

from __future__ import annotations

import logging
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from fraisier.dbops.confiture import (
    migrate_down,
    migrate_up,
    preflight,
)

log = logging.getLogger(__name__)


@dataclass
class StrategyResult:
    """Outcome of a database strategy execution."""

    success: bool
    migrations_applied: int = 0
    errors: list[str] = field(default_factory=list)


class Strategy(ABC):
    """Base class for database deployment strategies."""

    @abstractmethod
    def execute(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        allow_irreversible: bool = False,
        pre_migrate_verify: bool = False,
    ) -> StrategyResult: ...

    @abstractmethod
    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
    ) -> StrategyResult: ...


class MigrateStrategy(Strategy):
    """Production: preflight → migrate up.  Rollback via migrate down."""

    def execute(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        allow_irreversible: bool = False,
        pre_migrate_verify: bool = False,
    ) -> StrategyResult:
        preflight(
            confiture_config,
            migrations_dir=migrations_dir,
            allow_irreversible=allow_irreversible,
        )

        result = migrate_up(
            confiture_config,
            migrations_dir=migrations_dir,
            pre_migrate_verify=pre_migrate_verify,
            require_reversible=not allow_irreversible,
        )
        return StrategyResult(success=True, migrations_applied=result.steps_applied)

    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
    ) -> StrategyResult:
        result = migrate_down(
            confiture_config, migrations_dir=migrations_dir, steps=steps
        )
        return StrategyResult(
            success=result.success,
            migrations_applied=result.steps_applied,
            errors=result.errors,
        )


class RebuildStrategy(Strategy):
    """Development: rebuild database from scratch."""

    def execute(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        allow_irreversible: bool = False,
        pre_migrate_verify: bool = False,
    ) -> StrategyResult:
        from confiture.core.migrator import Migrator

        with Migrator.from_config(confiture_config, migrations_dir=migrations_dir) as m:
            m.rebuild(drop_schemas=True)
        return StrategyResult(success=True)

    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
    ) -> StrategyResult:
        return self.execute(confiture_config, migrations_dir=migrations_dir)


class RestoreMigrateStrategy(Strategy):
    """Staging: restore from backup, then migrate up.  Rollback via migrate down."""

    def __init__(self, restore_command: str) -> None:
        from fraisier.dbops._validation import validate_shell_command

        self._restore_tokens = validate_shell_command(restore_command)

    def execute(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        allow_irreversible: bool = False,
        pre_migrate_verify: bool = False,
    ) -> StrategyResult:
        subprocess.run(self._restore_tokens, check=True)

        result = migrate_up(confiture_config, migrations_dir=migrations_dir)
        return StrategyResult(success=True, migrations_applied=result.steps_applied)

    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
    ) -> StrategyResult:
        result = migrate_down(
            confiture_config, migrations_dir=migrations_dir, steps=steps
        )
        return StrategyResult(
            success=result.success,
            migrations_applied=result.steps_applied,
            errors=result.errors,
        )


def get_strategy(name: str, **kwargs: str) -> Strategy:
    """Factory for deployment strategies.

    Args:
        name: Strategy name (migrate, rebuild, restore_migrate).
        **kwargs: Extra args (e.g. restore_command for restore_migrate).
    """
    if name == "migrate":
        return MigrateStrategy()
    if name == "rebuild":
        return RebuildStrategy()
    if name == "restore_migrate":
        restore_cmd = kwargs.get("restore_command", "")
        if not restore_cmd:
            raise ValueError("restore_migrate strategy requires restore_command")
        return RestoreMigrateStrategy(restore_cmd)
    valid = "migrate, rebuild, restore_migrate"
    raise ValueError(f"Unknown strategy '{name}'. Valid: {valid}")

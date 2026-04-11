"""Shared result types and abstract base classes for strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StrategyResult:
    """Outcome of a database strategy execution."""

    success: bool
    migrations_applied: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ValidationResult:
    """Result of migration strategy validation."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class MigrationResult:
    """Result of migration operation."""

    success: bool
    migrations_applied: int = 0
    current_version: str | None = None
    target_version: str | None = None
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


class MigrationStrategy(ABC):
    """Enhanced strategy interface for multiple migration frameworks."""

    @property
    @abstractmethod
    def framework_name(self) -> str:
        """Name of the migration framework."""

    @abstractmethod
    def validate_setup(self, project_dir: Path) -> ValidationResult:
        """Validate migration setup and dependencies."""

    @abstractmethod
    def get_current_version(self, project_dir: Path) -> str | None:
        """Get current migration version."""

    @abstractmethod
    def get_latest_version(self, project_dir: Path) -> str | None:
        """Get latest available migration version."""

    @abstractmethod
    def migrate_up(
        self,
        project_dir: Path,
        target: str | None = None,
        database_url: str | None = None,
    ) -> MigrationResult:
        """Apply migrations to target version (default: latest)."""

    @abstractmethod
    def migrate_down(
        self, project_dir: Path, target: str, database_url: str | None = None
    ) -> MigrationResult:
        """Rollback migrations to target version."""

    @abstractmethod
    def get_migration_history(
        self, project_dir: Path, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get recent migration history."""


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
        database_url: str | None = None,
        hooks_config: dict[str, Any] | None = None,
    ) -> StrategyResult: ...

    @abstractmethod
    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
        database_url: str | None = None,
        hooks_config: dict[str, Any] | None = None,
    ) -> StrategyResult: ...

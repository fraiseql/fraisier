"""Peewee ORM migration strategy."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ._base import MigrationResult, MigrationStrategy, ValidationResult

log = logging.getLogger(__name__)


class PeeweeMigrateStrategy(MigrationStrategy):
    """Peewee ORM migration strategy."""

    def __init__(self, models_module: str, migrations_dir: str | Path):
        self.models_module = models_module
        self.migrations_dir = Path(migrations_dir)

    @property
    def framework_name(self) -> str:
        return "peewee"

    def validate_setup(self, project_dir: Path) -> ValidationResult:
        """Validate Peewee migration setup."""
        errors = []
        warnings = []

        # Check migrations directory exists
        migrations_path = project_dir / self.migrations_dir
        if not migrations_path.exists():
            errors.append(f"Peewee migrations directory not found: {migrations_path}")

        # Check Peewee is installed
        try:
            import importlib.util

            if importlib.util.find_spec("peewee") is None:  # type: ignore[attr-defined]
                raise ImportError("peewee not found")
        except ImportError:
            errors.append("peewee not installed")

        # Try to import models module
        if self.models_module:
            try:
                __import__(self.models_module)
            except ImportError:
                errors.append(
                    f"Cannot import Peewee models module: {self.models_module}"
                )

        return ValidationResult(
            valid=len(errors) == 0, errors=errors, warnings=warnings
        )

    def get_current_version(self, project_dir: Path) -> str | None:
        """Get current Peewee migration version."""
        # Peewee doesn't have a simple way to get current version: we'd
        # need to track this in a separate table or file. The body is
        # a placeholder return; there's no work that can raise, so the
        # bare try/except has been removed.
        return None

    def get_latest_version(self, project_dir: Path) -> str | None:
        """Get latest available Peewee migration."""
        try:
            migrations_path = project_dir / self.migrations_dir
            if not migrations_path.exists():
                return None

            # Find migration files (typically numbered Python files)
            migration_files = sorted(migrations_path.glob("*.py"))
            if migration_files:
                # Extract version from filename (assuming format like 0001_initial.py)
                latest_file = migration_files[-1]
                version = latest_file.stem.split("_")[0]
                try:
                    int(version)  # Validate it's numeric
                    return version
                except ValueError:
                    pass

            return None

        except (AttributeError, OSError) as e:
            # Expected modes: ``Path.glob`` raises OSError on
            # unreadable directories; pathlib API drift surfaces as
            # AttributeError. ``int(version)`` is already guarded by
            # an inner try/except. Anything else (e.g. a bug in our
            # filename parsing) propagates so it isn't masked.
            log.warning(f"Failed to get Peewee latest version: {e}")
            return None

    def migrate_up(
        self,
        project_dir: Path,
        target: str | None = None,
        database_url: str | None = None,
    ) -> MigrationResult:
        """Apply Peewee migrations."""
        try:
            # Import the models module to ensure database is set up
            if self.models_module:
                __import__(self.models_module)

            # Peewee migration execution is complex and depends on the specific setup
            # For now, we'll mark as not implemented and return a warning
            return MigrationResult(
                success=False,
                errors=["Peewee migration execution not yet implemented"],
                target_version=target or "latest",
            )

        except Exception as e:
            return MigrationResult(
                success=False, errors=[str(e)], target_version=target
            )

    def migrate_down(
        self, project_dir: Path, target: str, database_url: str | None = None
    ) -> MigrationResult:
        """Rollback Peewee migrations."""
        # Peewee rollback is also complex
        return MigrationResult(
            success=False,
            errors=["Peewee migration rollback not yet implemented"],
            target_version=target,
        )

    def get_migration_history(
        self, project_dir: Path, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get Peewee migration history."""
        try:
            migrations_path = project_dir / self.migrations_dir
            if not migrations_path.exists():
                return []

            history = []
            migration_files = sorted(migrations_path.glob("*.py"))[-limit:]

            for migration_file in migration_files:
                version = migration_file.stem.split("_")[0]
                name = "_".join(migration_file.stem.split("_")[1:])
                history.append(
                    {
                        "version": version,
                        "description": name.replace("_", " ").title(),
                        "applied": False,  # Peewee doesn't track applied status easily
                    }
                )

            return history

        except (AttributeError, OSError) as e:
            # Same expected modes as get_latest_version: filesystem
            # failures from ``Path.glob`` plus pathlib API drift.
            log.warning(f"Failed to get Peewee migration history: {e}")
            return []

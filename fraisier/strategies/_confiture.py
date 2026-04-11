"""Confiture migration strategy for FraiseQL and other frameworks."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import psycopg
import yaml

from ._base import MigrationResult, MigrationStrategy, ValidationResult

# Tuple used to narrow status-probe ``except`` blocks below.  See the
# justification on each call site for why each member is on the list.
_PROBE_ERRORS = (
    ImportError,
    AttributeError,
    OSError,
    ValueError,
    yaml.YAMLError,
    psycopg.Error,
)

# Import Migrator (optional dependency)
try:
    from confiture.core.migrator import Migrator
except ImportError:  # pragma: no cover
    Migrator = None  # type: ignore

log = logging.getLogger(__name__)


class ConfitureMigrateStrategy(MigrationStrategy):
    """Confiture migration strategy for FraiseQL and other frameworks."""

    def __init__(self, config_file: str | Path = "confiture.yaml"):
        self.config_file = Path(config_file)

    @property
    def framework_name(self) -> str:
        return "confiture"

    def validate_setup(self, project_dir: Path) -> ValidationResult:
        """Validate Confiture migration setup."""
        errors = []
        warnings = []

        config_path = project_dir / self.config_file
        if not config_path.exists():
            errors.append(f"Confiture config file not found: {config_path}")

        # Check Confiture is available
        try:
            import importlib.util

            if importlib.util.find_spec("confiture") is None:  # type: ignore[attr-defined]
                raise ImportError("confiture not found")
        except ImportError:
            errors.append("Confiture migration tools not available")

        return ValidationResult(
            valid=len(errors) == 0, errors=errors, warnings=warnings
        )

    def get_current_version(self, project_dir: Path) -> str | None:
        """Get current Confiture migration version."""
        try:
            from fraisier.dbops.confiture import _load_env

            env = _load_env(self.config_file)

            with Migrator.from_config(
                env, migrations_dir=project_dir / "db" / "migrations"
            ) as m:
                status = m.status()
                applied_versions = status.applied
                return applied_versions[-1] if applied_versions else None

        except _PROBE_ERRORS as e:
            # Status probe walks yaml -> pydantic -> confiture ->
            # psycopg.  Expected modes: missing/unreadable config
            # (OSError), bad YAML (yaml.YAMLError), pydantic
            # validation failure (ValueError), missing optional dep
            # (ImportError), API drift (AttributeError) and
            # database connection failure (psycopg.Error).  Anything
            # else propagates so genuine bugs aren't masked.
            log.warning(f"Failed to get current Confiture version: {e}")
            return None

    def get_latest_version(self, project_dir: Path) -> str | None:
        """Get latest available Confiture migration."""
        try:
            from fraisier.dbops.confiture import _load_env

            env = _load_env(self.config_file)

            with Migrator.from_config(
                env, migrations_dir=project_dir / "db" / "migrations"
            ) as m:
                status = m.status()
                pending_versions = status.pending
                if pending_versions:
                    return max(pending_versions)

                # If no pending, latest is the current
                applied_versions = status.applied
                return applied_versions[-1] if applied_versions else None

        except _PROBE_ERRORS as e:
            # Same expected modes as get_current_version: yaml /
            # pydantic / confiture / psycopg surface plus the usual
            # import / attribute / OS errors.
            log.warning(f"Failed to get latest Confiture version: {e}")
            return None

    def migrate_up(
        self,
        project_dir: Path,
        target: str | None = None,
        database_url: str | None = None,
    ) -> MigrationResult:
        """Apply Confiture migrations."""
        from fraisier.dbops.confiture import migrate_up

        try:
            config_path = project_dir / self.config_file
            migrations_dir = project_dir / "db" / "migrations"

            # Run preflight check
            from fraisier.dbops.confiture import preflight

            preflight(
                config_path, migrations_dir=migrations_dir, database_url=database_url
            )

            # Run migration
            result = migrate_up(
                config_path, migrations_dir=migrations_dir, database_url=database_url
            )

            return MigrationResult(
                success=result.success,
                migrations_applied=result.steps_applied,
                errors=result.errors,
                target_version=target or "latest",
            )

        except Exception as e:
            return MigrationResult(
                success=False, errors=[str(e)], target_version=target
            )

    def migrate_down(
        self,
        project_dir: Path,
        target: str,
        database_url: str | None = None,
        steps: int = 1,
    ) -> MigrationResult:
        """Rollback Confiture migrations."""
        from fraisier.dbops.confiture import migrate_down

        try:
            config_path = project_dir / self.config_file
            migrations_dir = project_dir / "db" / "migrations"

            # Run rollback
            result = migrate_down(
                config_path,
                migrations_dir=migrations_dir,
                steps=steps,
                database_url=database_url,
            )

            return MigrationResult(
                success=result.success,
                migrations_applied=result.steps_applied,
                errors=result.errors,
                target_version=target,
            )

        except Exception as e:
            return MigrationResult(
                success=False, errors=[str(e)], target_version=target
            )

    def get_migration_history(
        self, project_dir: Path, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get Confiture migration history."""
        try:
            from fraisier.dbops.confiture import _load_env

            env = _load_env(self.config_file)
            with Migrator.from_config(
                env, migrations_dir=project_dir / "db" / "migrations"
            ) as m:
                status = m.status()
                applied_migrations = [
                    mi for mi in status.migrations if mi.status == "applied"
                ]
                # Convert to our format
                history = [
                    {
                        "version": mi.version,
                        "applied": True,
                        "description": f"Confiture migration {mi.version}",
                        "timestamp": (
                            mi.applied_at.isoformat() if mi.applied_at else None
                        ),
                    }
                    for mi in applied_migrations[-limit:]
                ]
                return history

        except _PROBE_ERRORS as e:
            # Same expected modes as get_current_version: yaml /
            # pydantic / confiture / psycopg surface plus the usual
            # import / attribute / OS errors.
            log.warning(f"Failed to get Confiture migration history: {e}")
            return []

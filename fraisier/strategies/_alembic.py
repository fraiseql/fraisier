"""Alembic migration strategy for SQLAlchemy."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ._base import MigrationResult, MigrationStrategy, ValidationResult

log = logging.getLogger(__name__)


class AlembicMigrateStrategy(MigrationStrategy):
    """Alembic migration strategy for SQLAlchemy."""

    def __init__(
        self,
        script_location: str | Path,
        ini_path: str | Path,
        environment: str | None = None,
    ):
        self.script_location = Path(script_location)
        self.ini_path = Path(ini_path)
        self.environment = environment

    @property
    def framework_name(self) -> str:
        return "alembic"

    def validate_setup(self, project_dir: Path) -> ValidationResult:
        """Validate Alembic migration setup."""
        errors = []
        warnings = []

        # Check alembic.ini exists
        if not self.ini_path.exists():
            errors.append(f"alembic.ini not found: {self.ini_path}")

        # Check script location exists
        script_dir = project_dir / self.script_location
        if not script_dir.exists():
            errors.append(f"Alembic script location not found: {script_dir}")

        # Check env.py exists
        env_py = script_dir / "env.py"
        if not env_py.exists():
            errors.append(f"Alembic env.py not found: {env_py}")

        # Check alembic is installed
        try:
            import importlib.util

            if importlib.util.find_spec("alembic") is None:  # type: ignore[attr-defined]
                raise ImportError("alembic not found")
        except ImportError:
            errors.append("alembic not installed")

        return ValidationResult(
            valid=len(errors) == 0, errors=errors, warnings=warnings
        )

    def get_current_version(self, project_dir: Path) -> str | None:
        """Get current Alembic migration version."""
        try:
            import sys
            from io import StringIO

            from alembic import command
            from alembic.config import Config

            # Create alembic config
            config = Config(str(self.ini_path))
            config.set_main_option("script_location", str(self.script_location))

            # Capture output of current command
            old_stdout = sys.stdout
            sys.stdout = captured_output = StringIO()

            try:
                # Run alembic current
                command.current(config)
                output = captured_output.getvalue().strip()

                # Parse current revision from output
                # Output format: "Current revision(s) for 'main':\n123456789abc (head)"
                lines = output.split("\n")
                for line in lines:
                    if line.strip() and not line.startswith("Current revision"):
                        # Extract revision hash
                        revision = line.split()[0]
                        return revision

            finally:
                sys.stdout = old_stdout

        except (ImportError, AttributeError, OSError) as e:
            # Expected modes: alembic not installed (ImportError),
            # alembic API drift (AttributeError), or filesystem
            # failures reading alembic.ini / script_location
            # (OSError). Anything else — e.g. an alembic CommandError
            # for a misconfigured environment — propagates so the
            # underlying problem isn't silently masked.
            log.warning(f"Failed to get Alembic current version: {e}")

        return None

    def get_latest_version(self, project_dir: Path) -> str | None:
        """Get latest available Alembic migration."""
        try:
            from alembic import script
            from alembic.config import Config

            config = Config(str(self.ini_path))
            config.set_main_option("script_location", str(self.script_location))

            script_dir = script.ScriptDirectory.from_config(config)
            head_revision = script_dir.get_current_head()

            return head_revision

        except (ImportError, AttributeError, OSError) as e:
            # Same expected modes as get_current_version: missing
            # alembic, API drift, or unreadable script_location.
            log.warning(f"Failed to get Alembic latest version: {e}")

        return None

    def migrate_up(
        self,
        project_dir: Path,
        target: str | None = None,
        database_url: str | None = None,
    ) -> MigrationResult:
        """Apply Alembic migrations."""
        try:
            from alembic import command
            from alembic.config import Config

            # Create alembic config
            config = Config(str(self.ini_path))
            config.set_main_option("script_location", str(self.script_location))

            # Set database URL if provided
            if database_url:
                config.set_main_option("sqlalchemy.url", database_url)

            # Determine target revision
            target_revision = target or "head"

            # Execute upgrade
            command.upgrade(config, target_revision)

            return MigrationResult(
                success=True,
                migrations_applied=1,  # Alembic doesn't easily report count
                target_version=target_revision,
            )

        except Exception as e:
            return MigrationResult(
                success=False, errors=[str(e)], target_version=target
            )

    def migrate_down(
        self, project_dir: Path, target: str, database_url: str | None = None
    ) -> MigrationResult:
        """Rollback Alembic migrations."""
        try:
            from alembic import command
            from alembic.config import Config

            # Create alembic config
            config = Config(str(self.ini_path))
            config.set_main_option("script_location", str(self.script_location))

            # Set database URL if provided
            if database_url:
                config.set_main_option("sqlalchemy.url", database_url)

            # Execute downgrade
            command.downgrade(config, target)

            return MigrationResult(
                success=True,
                migrations_applied=1,  # Alembic doesn't easily report count
                target_version=target,
            )

        except Exception as e:
            return MigrationResult(
                success=False, errors=[str(e)], target_version=target
            )

    def get_migration_history(
        self, project_dir: Path, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get Alembic migration history."""
        try:
            from alembic import script
            from alembic.config import Config

            config = Config(str(self.ini_path))
            config.set_main_option("script_location", str(self.script_location))

            script_dir = script.ScriptDirectory.from_config(config)

            # Get revision history
            history = []
            for revision in script_dir.walk_revisions():
                history.append(
                    {
                        "version": revision.revision,
                        "description": revision.doc or f"Migration {revision.revision}",
                        "applied": False,  # Alembic doesn't track applied status easily
                    }
                )
                if len(history) >= limit:
                    break

            return history

        except (ImportError, AttributeError, OSError) as e:
            # Walking the script directory: failures here are missing
            # alembic, API drift in walk_revisions, or unreadable
            # script_location. Anything else propagates.
            log.warning(f"Failed to get Alembic migration history: {e}")
            return []

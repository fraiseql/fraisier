"""Django migration strategy."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ._base import MigrationResult, MigrationStrategy, ValidationResult

log = logging.getLogger(__name__)


class DjangoMigrateStrategy(MigrationStrategy):
    """Django migration strategy."""

    def __init__(self, settings_module: str, app_label: str | None = None):
        self.settings_module = settings_module
        self.app_label = app_label

    @property
    def framework_name(self) -> str:
        return "django"

    def validate_setup(self, project_dir: Path) -> ValidationResult:
        """Validate Django migration setup."""
        errors = []
        warnings = []

        # Check manage.py exists
        manage_py = project_dir / "manage.py"
        if not manage_py.exists():
            errors.append("manage.py not found")

        # Check Django is installed
        try:
            import django
        except ImportError:
            errors.append("Django not installed")

        # Check settings module can be imported
        if not errors:
            try:
                import os

                os.environ.setdefault("DJANGO_SETTINGS_MODULE", self.settings_module)
                import django

                django.setup()
            except Exception as e:
                # Bound-broad: ``django.setup()`` can raise
                # ``ImproperlyConfigured`` (subclass of Exception) plus
                # arbitrary errors raised inside the user's settings
                # module. This block is the *validation* safety net —
                # its job is to convert any setup failure into a
                # collected error string, not to crash the validator.
                errors.append(
                    f"Cannot setup Django with settings module "
                    f"'{self.settings_module}': {e}"
                )

        return ValidationResult(
            valid=len(errors) == 0, errors=errors, warnings=warnings
        )

    def get_current_version(self, project_dir: Path) -> str | None:
        """Get current Django migration version."""
        try:
            import sys
            from io import StringIO

            from django.core.management import execute_from_command_line

            # Capture output of showmigrations
            old_stdout = sys.stdout
            sys.stdout = captured_output = StringIO()

            try:
                # Run showmigrations command
                if self.app_label:
                    execute_from_command_line(
                        ["manage.py", "showmigrations", self.app_label]
                    )
                else:
                    execute_from_command_line(["manage.py", "showmigrations"])

                output = captured_output.getvalue()
                # Parse the last applied migration
                lines = output.strip().split("\n")
                applied_migrations = [
                    line.strip()
                    for line in lines
                    if line.strip().endswith("]") and "[" in line
                ]

                if applied_migrations:
                    # Return the last applied migration name
                    last_migration = applied_migrations[-1]
                    # Extract migration name from [X] format
                    if "[" in last_migration and "]" in last_migration:
                        return last_migration.split("]")[0].split("[")[-1].strip()

            finally:
                sys.stdout = old_stdout

        except (ImportError, AttributeError, OSError) as e:
            # Expected modes when probing migration state: Django not
            # installed (ImportError), showmigrations API drift
            # (AttributeError), or I/O failures spawning ``manage.py``
            # (OSError). Anything else — Django ``CommandError``,
            # ``ImproperlyConfigured`` from a project that skipped
            # ``validate_setup`` — propagates so a real misuse or
            # adapter bug isn't silently masked.
            log.warning(f"Failed to get Django migration version: {e}")

        return None

    def get_latest_version(self, project_dir: Path) -> str | None:
        """Get latest available Django migration."""
        try:
            from django.apps import apps
            from django.db import migrations

            if self.app_label:
                app_config = apps.get_app_config(self.app_label)
                migration_module = migrations.get_migration_module(app_config)  # ty: ignore[unresolved-attribute]
                # Get the latest migration name
                migration_names = [
                    name for name in dir(migration_module) if not name.startswith("_")
                ]
                if migration_names:
                    return max(migration_names)  # Assuming lexical ordering
            else:
                # Check all apps for latest migration
                latest_migration = None
                for app_config in apps.get_app_configs():
                    try:
                        migration_module = migrations.get_migration_module(app_config)  # ty: ignore[unresolved-attribute]
                        migration_names = [
                            name
                            for name in dir(migration_module)
                            if not name.startswith("_")
                        ]
                        if migration_names:
                            app_latest = max(migration_names)
                            if (
                                latest_migration is None
                                or app_latest > latest_migration
                            ):
                                latest_migration = app_latest
                    except (ImportError, AttributeError, OSError):
                        # Expected failure modes for an app whose migrations
                        # module is missing, broken, or on an unreadable path.
                        # Anything else (e.g. a real bug) propagates to the
                        # outer warning handler so it isn't silently masked.
                        continue
                return latest_migration

        except Exception as e:
            # Outer safety net: this catches *anything* the inner loop
            # didn't handle. The inner loop deliberately narrows to
            # (ImportError, AttributeError, OSError) so that real bugs
            # (e.g. a TypeError from a refactor) propagate here and
            # surface as a warning instead of being silently skipped.
            log.warning(f"Failed to get latest Django migration: {e}")

        return None

    def migrate_up(
        self,
        project_dir: Path,
        target: str | None = None,
        database_url: str | None = None,
    ) -> MigrationResult:
        """Apply Django migrations."""
        try:
            import os

            from django.core.management import execute_from_command_line

            # Set working directory
            old_cwd = Path.cwd()
            os.chdir(project_dir)

            try:
                # Build migrate command
                cmd = ["manage.py", "migrate"]
                if self.app_label:
                    cmd.append(self.app_label)
                if target:
                    cmd.append(target)

                # Execute migration
                execute_from_command_line(cmd)

                return MigrationResult(
                    success=True,
                    migrations_applied=1,  # Django doesn't report count easily
                    target_version=target or "latest",
                )

            finally:
                os.chdir(old_cwd)

        except Exception as e:
            return MigrationResult(
                success=False, errors=[str(e)], target_version=target
            )

    def migrate_down(
        self, project_dir: Path, target: str, database_url: str | None = None
    ) -> MigrationResult:
        """Rollback Django migrations."""
        try:
            import os

            from django.core.management import execute_from_command_line

            # Set working directory
            old_cwd = Path.cwd()
            os.chdir(project_dir)

            try:
                # Build migrate command for rollback
                cmd = ["manage.py", "migrate"]
                if self.app_label:
                    cmd.extend([self.app_label, target])
                else:
                    # For all apps, we need to specify target differently
                    cmd.append(target)

                # Execute rollback
                execute_from_command_line(cmd)

                return MigrationResult(
                    success=True,
                    migrations_applied=1,  # Django doesn't report count easily
                    target_version=target,
                )

            finally:
                os.chdir(old_cwd)

        except Exception as e:
            return MigrationResult(
                success=False, errors=[str(e)], target_version=target
            )

    def get_migration_history(
        self, project_dir: Path, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Get Django migration history."""
        # Django doesn't have a simple way to get migration history
        # This is a simplified implementation
        try:
            current = self.get_current_version(project_dir)
            latest = self.get_latest_version(project_dir)

            history = []
            if current:
                history.append(
                    {
                        "version": current,
                        "applied": True,
                        "description": f"Django migration {current}",
                    }
                )
            if latest and latest != current:
                history.append(
                    {
                        "version": latest,
                        "applied": False,
                        "description": f"Django migration {latest}",
                    }
                )

            return history[:limit]

        except (ImportError, AttributeError, OSError) as e:
            # History is derived from get_current_version /
            # get_latest_version (which already swallow their own
            # narrow set of errors); the residual surface here is
            # framework imports / API drift / filesystem failures.
            log.warning(f"Failed to get Django migration history: {e}")
            return []

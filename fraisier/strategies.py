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
from typing import Any

from fraisier.dbops._validation import validate_pg_identifier
from fraisier.dbops.confiture import (
    migrate_down,
    migrate_up,
    preflight,
)
from fraisier.dbops.operations import (
    create_db,
    drop_db,
    run_psql,
    terminate_backends,
)

# Import Migrator for Confiture strategy (optional import)
try:
    from confiture.core.migrator import Migrator
except ImportError:
    Migrator = None  # type: ignore

log = logging.getLogger(__name__)


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
    ) -> StrategyResult: ...

    @abstractmethod
    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
        database_url: str | None = None,
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
        database_url: str | None = None,
    ) -> StrategyResult:
        preflight(
            confiture_config,
            migrations_dir=migrations_dir,
            allow_irreversible=allow_irreversible,
            database_url=database_url,
        )

        result = migrate_up(
            confiture_config,
            migrations_dir=migrations_dir,
            pre_migrate_verify=pre_migrate_verify,
            require_reversible=not allow_irreversible,
            database_url=database_url,
        )
        return StrategyResult(success=True, migrations_applied=result.steps_applied)

    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
        database_url: str | None = None,
    ) -> StrategyResult:
        result = migrate_down(
            confiture_config,
            migrations_dir=migrations_dir,
            steps=steps,
            database_url=database_url,
        )
        return StrategyResult(
            success=result.success,
            migrations_applied=result.steps_applied,
            errors=result.errors,
        )


class RebuildStrategy(Strategy):
    """Development: rebuild database from scratch.

    Uses ``confiture build`` (SchemaBuilder) to generate the full SQL
    (DDL + seeds), drops existing schemas via ``psql``, applies the
    generated file in bulk (single protocol message — 10-50x faster than
    per-statement execution), then re-baselines the migration tracking
    table.

    When *required_roles* is configured, those roles are provisioned
    (``CREATE ROLE … NOLOGIN``) and granted to the database owner
    **before** the schema is applied.  This prevents silent failures
    when the schema contains ``CREATE SCHEMA … AUTHORIZATION <role>``
    for roles that don't yet exist on the cluster.
    """

    def __init__(
        self,
        *,
        required_roles: list[str] | None = None,
        project_dir: Path | None = None,
        admin_url: str | None = None,
    ) -> None:
        self._required_roles: list[str] = []
        for role in required_roles or []:
            validate_pg_identifier(role, "required role")
            self._required_roles.append(role)
        self._project_dir = project_dir
        self._admin_url = admin_url

    @staticmethod
    def _apply_sql(connection_url: str, sql_path: Path) -> None:
        """Apply a SQL file via psql with ON_ERROR_STOP."""
        psql_cmd = [
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            connection_url,
            "-f",
            str(sql_path),
        ]
        result = subprocess.run(
            psql_cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            log.error("psql failed on %s:\n%s", sql_path, result.stderr)
            raise subprocess.CalledProcessError(
                result.returncode,
                "psql",
                output=result.stdout,
                stderr=result.stderr,
            )

    def _provision_roles(
        self,
        db_name: str,
        db_owner: str | None,
        *,
        connection_url: str | None = None,
    ) -> None:
        """Ensure required roles exist and are granted to the db owner."""
        for role in self._required_roles:
            sql = (
                "DO $$ BEGIN "
                f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') "
                f"THEN CREATE ROLE {role} NOLOGIN; END IF; END $$;"
            )
            code, _, stderr = run_psql(
                sql, db_name=db_name, connection_url=connection_url
            )
            if code != 0:
                raise subprocess.CalledProcessError(code, "psql", stderr=stderr)
            log.info("Ensured role %s exists", role)

            if db_owner:
                grant_sql = f"GRANT {role} TO {db_owner}"
                code, _, stderr = run_psql(
                    grant_sql,
                    db_name=db_name,
                    connection_url=connection_url,
                )
                if code != 0:
                    raise subprocess.CalledProcessError(code, "psql", stderr=stderr)
                log.info("Granted %s to %s", role, db_owner)

    def execute(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        allow_irreversible: bool = False,
        pre_migrate_verify: bool = False,
        database_url: str | None = None,
    ) -> StrategyResult:
        import tempfile
        from urllib.parse import urlparse

        import yaml
        from confiture.config.environment import Environment
        from confiture.core.builder import SchemaBuilder

        # Load environment from config YAML.
        raw: dict = yaml.safe_load(  # type: ignore[assignment]
            Path(confiture_config).read_text()
        )
        if database_url:
            raw["database_url"] = database_url
        env = Environment.model_validate(raw)

        # Parse database name and owner from the connection URL.
        parsed = urlparse(env.database_url)
        db_name = parsed.path.lstrip("/")
        db_owner = parsed.username

        # Admin URL for privileged operations (DROP/CREATE DATABASE).
        # Priority: explicit admin_url > derived from database_url > sudo.
        from fraisier.dbops._url import replace_db_name

        admin_url: str | None = self._admin_url
        if not admin_url and database_url:
            admin_url = replace_db_name(env.database_url, "postgres")

        # Build SQL split into superuser and app phases.
        # project_dir must be the app root so SchemaBuilder can find
        # db/environments/<name>.yaml relative to it.  When called via
        # the deployer, app_path is passed explicitly; when called
        # directly (e.g. integration tests), fall back to the config
        # file's parent (works when config sits at the project root).
        project_dir = self._project_dir or Path(confiture_config).resolve().parent
        builder = SchemaBuilder(env=env.name, project_dir=project_dir)
        output_dir = Path(tempfile.mkdtemp(prefix="fraisier_rebuild_"))

        try:
            split = builder.build_split(output_dir=output_dir)
            superuser_pre_path = Path(split.superuser_pre_path)
            app_path = Path(split.app_path)

            # Drop and recreate the database as postgres superuser.
            terminate_backends(db_name, connection_url=admin_url)
            drop_db(db_name, connection_url=admin_url)
            code, _, stderr = create_db(
                db_name, owner=db_owner, connection_url=admin_url
            )
            if code != 0:
                raise subprocess.CalledProcessError(code, "createdb", stderr=stderr)

            # Provision required roles before schema apply so that
            # CREATE SCHEMA … AUTHORIZATION <role> doesn't fail.
            if self._required_roles:
                self._provision_roles(db_name, db_owner, connection_url=admin_url)

            # Phase 1: Apply superuser SQL (roles, extensions) via admin_url.
            # Compute the admin connection URL targeting the app database.
            # Superuser SQL must land in the app db, not postgres.
            admin_app_conn = replace_db_name(admin_url, db_name) if admin_url else None

            # Phase 1: Apply superuser pre-schema SQL (roles, extensions).
            if split.superuser_pre_files > 0:
                su_conn = admin_app_conn or env.database_url
                self._apply_sql(su_conn, superuser_pre_path)

            # Phase 2: Apply app SQL (schemas, tables, views, data).
            self._apply_sql(env.database_url, app_path)

            # Phase 3: Apply superuser post-schema SQL (grants on tables,
            # role settings) — requires tables to exist, hence after app.
            if split.superuser_post_files > 0:
                su_post_conn = admin_app_conn or env.database_url
                self._apply_sql(su_post_conn, Path(split.superuser_post_path))
        finally:
            import shutil

            shutil.rmtree(output_dir, ignore_errors=True)

        # Re-baseline migration tracking table.
        with Migrator.from_config(env, migrations_dir=migrations_dir) as m:
            m.reinit()

        return StrategyResult(success=True)

    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
        database_url: str | None = None,
    ) -> StrategyResult:
        return self.execute(
            confiture_config, migrations_dir=migrations_dir, database_url=database_url
        )


@dataclass
class RestoreConfig:
    """Structured configuration for the restore_migrate strategy."""

    db_name: str
    backup_dir: Path
    backup_pattern: str = "*.dump"
    max_age_hours: float = 48.0
    target_owner: str | None = None
    create_template: bool = False
    template_name: str | None = None
    min_tables: int = 0


class RestoreMigrateStrategy(Strategy):
    """Staging: full backup restore lifecycle, then migrate up.

    Steps:
    1. Find latest backup matching pattern in backup_dir
    2. Validate backup age (< max_age_hours)
    3. Terminate all connections to target database
    4. DROP DATABASE IF EXISTS + CREATE DATABASE
    5. pg_restore --no-owner --no-acl
    6. REASSIGN OWNED to target_owner (if configured)
    7. CREATE DATABASE template (if create_template=true)
    8. confiture migrate up
    9. Validate table count >= min_tables (if configured)

    Rollback: template-based (instant) or migrate_down.
    """

    def __init__(self, config: RestoreConfig, *, admin_url: str | None = None) -> None:
        from fraisier.dbops._validation import validate_pg_identifier

        validate_pg_identifier(config.db_name, "database name")
        if config.target_owner:
            validate_pg_identifier(config.target_owner, "target owner")
        if config.template_name:
            validate_pg_identifier(config.template_name, "template name")
        self._config = config
        self._admin_url = admin_url

    @property
    def _resolved_template_name(self) -> str:
        return self._config.template_name or f"template_{self._config.db_name}"

    def execute(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        allow_irreversible: bool = False,
        pre_migrate_verify: bool = False,
        database_url: str | None = None,
    ) -> StrategyResult:
        from fraisier.dbops.operations import create_db, drop_db, terminate_backends
        from fraisier.dbops.restore import (
            find_latest_backup,
            restore_backup,
            validate_backup_age,
            validate_table_count,
        )
        from fraisier.errors import DatabaseError

        cfg = self._config

        # Step 1: Find latest backup
        backup_file = find_latest_backup(cfg.backup_dir, pattern=cfg.backup_pattern)
        if backup_file is None:
            raise DatabaseError(
                f"No backup matching '{cfg.backup_pattern}' in {cfg.backup_dir}",
            )
        log.info("Found backup: %s", backup_file)

        # Step 2: Validate backup age
        if not validate_backup_age(backup_file, max_age_hours=cfg.max_age_hours):
            raise DatabaseError(
                f"Backup {backup_file.name} is older than {cfg.max_age_hours}h",
            )

        # Step 3: Terminate connections
        terminate_backends(cfg.db_name, connection_url=self._admin_url)
        log.info("Terminated connections to %s", cfg.db_name)

        # Step 4: Drop and recreate database
        drop_db(cfg.db_name, connection_url=self._admin_url)
        code, _, stderr = create_db(cfg.db_name, connection_url=self._admin_url)
        if code != 0:
            raise DatabaseError(
                f"Failed to create database {cfg.db_name}: {stderr.strip()}",
            )
        log.info("Recreated database %s", cfg.db_name)

        # Step 5 + 6: pg_restore (with optional ownership fix)
        restore_result = restore_backup(
            backup_path=str(backup_file),
            db_name=cfg.db_name,
            db_owner=cfg.target_owner,
        )
        if not restore_result.success:
            raise DatabaseError(
                f"pg_restore failed: {restore_result.error}",
            )
        log.info("Restored backup into %s", cfg.db_name)

        # Step 7: Create rollback template
        if cfg.create_template:
            template_name = self._resolved_template_name
            # Drop existing template if any, disconnect from source, create
            terminate_backends(template_name, connection_url=self._admin_url)
            drop_db(template_name, connection_url=self._admin_url)
            terminate_backends(cfg.db_name, connection_url=self._admin_url)
            code, _, stderr = create_db(
                template_name, template=cfg.db_name, connection_url=self._admin_url
            )
            if code != 0:
                raise DatabaseError(
                    f"Failed to create template {template_name}: {stderr.strip()}",
                )
            log.info("Created rollback template %s", template_name)

        # Step 8: Migrate up
        result = migrate_up(
            confiture_config, migrations_dir=migrations_dir, database_url=database_url
        )
        log.info("Applied %d migrations", result.steps_applied)

        # Step 9: Validate table count
        if cfg.min_tables > 0:
            ok, count = validate_table_count(cfg.db_name, min_threshold=cfg.min_tables)
            if not ok:
                raise DatabaseError(
                    f"Table count validation failed: {count} < {cfg.min_tables}",
                )
            log.info("Table count validation passed: %d >= %d", count, cfg.min_tables)

        return StrategyResult(success=True, migrations_applied=result.steps_applied)

    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
        database_url: str | None = None,
    ) -> StrategyResult:
        if self._config.create_template:
            from fraisier.dbops.templates import reset_from_template

            template_name = self._resolved_template_name
            # Compute the prefix that makes prefix + db_name == template_name
            prefix = template_name.removesuffix(self._config.db_name)
            if prefix + self._config.db_name != template_name:
                # Custom template name doesn't follow prefix convention —
                # do drop + create manually.
                from fraisier.dbops.operations import (
                    create_db,
                    drop_db,
                    terminate_backends,
                )

                terminate_backends(self._config.db_name, connection_url=self._admin_url)
                drop_db(self._config.db_name, connection_url=self._admin_url)
                terminate_backends(template_name, connection_url=self._admin_url)
                code, _, stderr = create_db(
                    self._config.db_name,
                    template=template_name,
                    connection_url=self._admin_url,
                )
                if code != 0:
                    return StrategyResult(
                        success=False,
                        errors=[f"Template rollback failed: {stderr.strip()}"],
                    )
                return StrategyResult(success=True)

            tmpl_result = reset_from_template(self._config.db_name, prefix=prefix)
            if not tmpl_result.success:
                return StrategyResult(
                    success=False,
                    errors=[f"Template rollback failed: {tmpl_result.error}"],
                )
            return StrategyResult(success=True)

        result = migrate_down(
            confiture_config,
            migrations_dir=migrations_dir,
            steps=steps,
            database_url=database_url,
        )
        return StrategyResult(
            success=result.success,
            migrations_applied=result.steps_applied,
            errors=result.errors,
        )


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
        config = RestoreConfig(
            db_name=db_name,
            backup_dir=Path(restore_cfg["backup_dir"]),
            backup_pattern=restore_cfg.get("backup_pattern", "*.dump"),
            max_age_hours=float(restore_cfg.get("max_age_hours", 48.0)),
            target_owner=restore_cfg.get("target_owner"),
            create_template=bool(restore_cfg.get("create_template", False)),
            template_name=restore_cfg.get("template_name"),
            min_tables=int(restore_cfg.get("min_tables", 0)),
        )
        admin_url = kwargs.get("admin_url")
        return RestoreMigrateStrategy(config, admin_url=admin_url)
    valid = "migrate, rebuild, restore_migrate"
    raise ValueError(f"Unknown strategy '{name}'. Valid: {valid}")


# Migration Framework Strategies


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

        except Exception as e:
            log.warning(f"Failed to get Django migration version: {e}")

        return None

    def get_latest_version(self, project_dir: Path) -> str | None:
        """Get latest available Django migration."""
        try:
            from django.apps import apps
            from django.db import migrations

            if self.app_label:
                app_config = apps.get_app_config(self.app_label)
                migration_module = migrations.get_migration_module(app_config)
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
                        migration_module = migrations.get_migration_module(app_config)
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
                    except Exception:
                        continue
                return latest_migration

        except Exception as e:
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

        except Exception as e:
            log.warning(f"Failed to get Django migration history: {e}")
            return []


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

        except Exception as e:
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

        except Exception as e:
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

        except Exception as e:
            log.warning(f"Failed to get Alembic migration history: {e}")
            return []


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
        try:
            # Peewee doesn't have a simple way to get current version
            # We'd need to track this in a separate table or file
            # For now, return None (not implemented)
            return None

        except Exception as e:
            log.warning(f"Failed to get Peewee current version: {e}")
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

        except Exception as e:
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

        except Exception as e:
            log.warning(f"Failed to get Peewee migration history: {e}")
            return []


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
                applied_versions = m.get_applied_versions()
                return applied_versions[-1] if applied_versions else None

        except Exception as e:
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
                pending_files = m.find_pending()
                if pending_files:
                    # Extract version from filename
                    # (format: YYYYMMDDHHMMSS_description.up.sql)
                    latest_file = max(pending_files)
                    # Version is the timestamp prefix
                    version = latest_file.name.split("_", 1)[0]
                    return version

                # If no pending, latest is the current
                applied_versions = m.get_applied_versions()
                return applied_versions[-1] if applied_versions else None

        except Exception as e:
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
        self, project_dir: Path, target: str, database_url: str | None = None
    ) -> MigrationResult:
        """Rollback Confiture migrations."""
        from fraisier.dbops.confiture import migrate_down

        try:
            config_path = project_dir / self.config_file
            migrations_dir = project_dir / "db" / "migrations"

            # Run rollback
            result = migrate_down(
                config_path, migrations_dir=migrations_dir, database_url=database_url
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
                applied = m.get_applied_migrations_with_timestamps()
                # Convert to our format
                history = [
                    {
                        "version": migration["version"],
                        "applied": True,
                        "description": (f"Confiture migration {migration['version']}"),
                        "timestamp": migration.get("applied_at"),
                    }
                    for migration in applied[-limit:]
                ]
                return history

        except Exception as e:
            log.warning(f"Failed to get Confiture migration history: {e}")
            return []

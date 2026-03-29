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

from fraisier.dbops.confiture import (
    migrate_down,
    migrate_up,
    preflight,
)
from fraisier.dbops.operations import create_db, drop_db, terminate_backends

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
    """

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
        from urllib.parse import urlparse, urlunparse

        import yaml
        from confiture.config.environment import Environment
        from confiture.core.builder import SchemaBuilder
        from confiture.core.migrator import Migrator

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

        # Derive a superuser admin URL (pointing at the "postgres"
        # database) when a connection_url was provided.  This lets
        # terminate_backends / drop_db / create_db connect without sudo.
        admin_url: str | None = None
        if database_url:
            admin_url = urlunparse(parsed._replace(path="/postgres"))

        # Build full SQL (DDL + seeds).
        project_dir = Path(confiture_config).resolve().parent
        builder = SchemaBuilder(env=env.name, project_dir=project_dir)
        with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as tmp:
            output_path = Path(tmp.name)

        try:
            builder.build(output_path=output_path)

            # Drop and recreate the database as postgres superuser.
            # This avoids "must be owner of schema public" errors when
            # the app user doesn't own the public schema.
            terminate_backends(db_name, connection_url=admin_url)
            drop_db(db_name, connection_url=admin_url)
            code, _, stderr = create_db(
                db_name, owner=db_owner, connection_url=admin_url
            )
            if code != 0:
                raise subprocess.CalledProcessError(code, "createdb", stderr=stderr)

            # Apply the generated schema in one shot (fast).
            result = subprocess.run(
                ["psql", env.database_url, "-f", str(output_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise subprocess.CalledProcessError(
                    result.returncode, "psql", stderr=result.stderr
                )
        finally:
            output_path.unlink(missing_ok=True)

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

    def __init__(self, config: RestoreConfig) -> None:
        from fraisier.dbops._validation import validate_pg_identifier

        validate_pg_identifier(config.db_name, "database name")
        if config.target_owner:
            validate_pg_identifier(config.target_owner, "target owner")
        if config.template_name:
            validate_pg_identifier(config.template_name, "template name")
        self._config = config

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
        terminate_backends(cfg.db_name)
        log.info("Terminated connections to %s", cfg.db_name)

        # Step 4: Drop and recreate database
        drop_db(cfg.db_name)
        code, _, stderr = create_db(cfg.db_name)
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
            terminate_backends(template_name)
            drop_db(template_name)
            terminate_backends(cfg.db_name)
            code, _, stderr = create_db(template_name, template=cfg.db_name)
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

                terminate_backends(self._config.db_name)
                drop_db(self._config.db_name)
                terminate_backends(template_name)
                code, _, stderr = create_db(
                    self._config.db_name, template=template_name
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
        return RebuildStrategy()
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
        return RestoreMigrateStrategy(config)
    valid = "migrate, rebuild, restore_migrate"
    raise ValueError(f"Unknown strategy '{name}'. Valid: {valid}")

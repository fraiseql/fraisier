"""Staging strategy: full backup restore lifecycle, then migrate up."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fraisier.config.schema import PreflightConfig
from fraisier.dbops.confiture import migrate_down, migrate_up

from ._base import Strategy, StrategyResult

log = logging.getLogger(__name__)


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
    jobs: int = 1
    preferred_compression: str | None = None
    backup_path: Path | None = None
    preflight: PreflightConfig = field(default_factory=PreflightConfig)


class RestoreMigrateStrategy(Strategy):
    """Staging: full backup restore lifecycle, then migrate up.

    Steps:
    1. Find latest backup matching pattern in backup_dir
    2. Validate backup age (< max_age_hours)
    3. Stop service (if service_name configured, prevents connection reconnect)
    4. Terminate all connections to target database
    5. DROP DATABASE IF EXISTS + CREATE DATABASE
    6. pg_restore --no-owner --no-acl
    7. REASSIGN OWNED to target_owner (if configured)
    8. CREATE DATABASE template (if create_template=true)
    9. confiture migrate up
    10. Validate table count >= min_tables (if configured)
    11. Start service (if service_name configured)

    Rollback: template-based (instant) or migrate_down.
    """

    def __init__(
        self,
        config: RestoreConfig,
        *,
        admin_url: str,
        service_manager=None,
        service_name: str | None = None,
    ) -> None:
        from fraisier.dbops._validation import validate_pg_identifier

        validate_pg_identifier(config.db_name, "database name")
        if config.target_owner:
            validate_pg_identifier(config.target_owner, "target owner")
        if config.template_name:
            validate_pg_identifier(config.template_name, "template name")
        self._config = config
        self._admin_url = admin_url
        self._service_manager = service_manager
        self._service_name = service_name

    @property
    def _resolved_template_name(self) -> str:
        return self._config.template_name or f"template_{self._config.db_name}"

    def _preflight_enabled(self) -> bool:
        """Return True when the migration preflight check should run."""
        return self._config.preflight.enabled

    def _run_preflight(
        self,
        backup_path: Path,
        confiture_config: Path,
        migrations_dir: Path,
    ) -> None:
        """Run migration preflight. Raises MigrationPreflightError on failure.

        Args:
            backup_path: Path to the pg_dump backup file.
            confiture_config: Path to the confiture config file.
            migrations_dir: Directory containing migration files.

        Raises:
            MigrationPreflightError: When one or more migrations would fail,
                with a structured result attached for programmatic use.
        """
        from fraisier.dbops.preflight import run_migration_preflight
        from fraisier.errors import MigrationPreflightError

        pf = self._config.preflight
        log.info("Running migration preflight check...")
        result = run_migration_preflight(
            backup_path=backup_path,
            admin_url=self._admin_url,
            confiture_config=confiture_config,
            migrations_dir=migrations_dir,
            timeout_seconds=pf.timeout_seconds,
        )

        if result.all_passed:
            log.info(
                "Preflight passed: %d migrations validated in %dms",
                len(result.migrations),
                result.total_ms,
            )
        else:
            failures = "\n".join(
                f"  - {m.version} ({m.name}): {m.error}" for m in result.failures
            )
            raise MigrationPreflightError(
                f"Migration preflight failed ({result.failure_count} of "
                f"{len(result.migrations)} migrations would fail):\n{failures}",
                preflight_result=result,
            )

    def execute(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        allow_irreversible: bool = False,
        pre_migrate_verify: bool = False,
        database_url: str | None = None,
        hooks_config: dict[str, Any] | None = None,
        skip_preflight: bool = False,
    ) -> StrategyResult:
        from fraisier.dbops.operations import (
            create_db,
            drop_db,
            terminate_backends,
        )
        from fraisier.dbops.restore import (
            find_latest_backup,
            restore_backup,
            validate_backup_age,
            validate_table_count,
        )
        from fraisier.errors import DatabaseError

        cfg = self._config

        # Step 1: Resolve backup file
        if cfg.backup_path is not None:
            backup_file = cfg.backup_path
            log.info("Using explicit backup: %s", backup_file)
        else:
            backup_file = find_latest_backup(
                cfg.backup_dir,
                pattern=cfg.backup_pattern,
                preferred_compression=cfg.preferred_compression,
            )
            if backup_file is None:
                raise DatabaseError(
                    f"No backup matching '{cfg.backup_pattern}' in {cfg.backup_dir}",
                )
            log.info("Found backup: %s", backup_file)

            # Step 2: Validate backup age (only when not explicit)
            if not validate_backup_age(backup_file, max_age_hours=cfg.max_age_hours):
                raise DatabaseError(
                    f"Backup {backup_file.name} is older than {cfg.max_age_hours}h",
                )

        # Step 2.5: Preflight check (before any destructive operations)
        # Service is still running here — preflight only uses a temp DB.
        if not skip_preflight and self._preflight_enabled():
            self._run_preflight(
                backup_path=backup_file,
                confiture_config=confiture_config,
                migrations_dir=migrations_dir,
            )

        # Step 3: Stop service to prevent connection reconnect race
        if self._service_manager and self._service_name:
            try:
                self._service_manager.stop(self._service_name)
                self._service_manager.wait_stopped(self._service_name)
            except Exception as exc:
                raise DatabaseError(
                    f"Failed to stop service {self._service_name}: {exc}"
                ) from exc
            log.info("Stopped service %s", self._service_name)

        # Step 4: Terminate connections
        terminate_backends(cfg.db_name, connection_url=self._admin_url)
        log.info("Terminated connections to %s", cfg.db_name)

        # Step 5: Drop and recreate database
        code, _, stderr = drop_db(
            cfg.db_name, force=True, connection_url=self._admin_url
        )
        if code != 0:
            raise DatabaseError(
                f"Failed to drop database {cfg.db_name}: {stderr.strip()}",
            )
        code, _, stderr = create_db(cfg.db_name, connection_url=self._admin_url)
        if code != 0:  # pragma: no cover
            raise DatabaseError(
                f"Failed to create database {cfg.db_name}: {stderr.strip()}",
            )
        log.info("Recreated database %s", cfg.db_name)

        # Step 6 + 7: pg_restore (with optional ownership fix)
        t_total = time.monotonic()
        restore_result = restore_backup(
            backup_path=str(backup_file),
            db_name=cfg.db_name,
            db_owner=cfg.target_owner,
            connection_url=self._admin_url,
            jobs=cfg.jobs,
        )
        restore_secs = restore_result.duration_seconds
        if not restore_result.success:
            raise DatabaseError(
                f"pg_restore failed: {restore_result.error}",
            )
        log.info(
            "Restored backup into %s (%dms)",
            cfg.db_name,
            int(restore_secs * 1000),
        )

        # Step 8: Create rollback template
        if cfg.create_template:
            template_name = self._resolved_template_name
            # Drop existing template if any, disconnect from source, create.
            # clear_template_flag: Postgres refuses to drop a database with
            # datistemplate=true (even WITH FORCE); fixes #200 re-deploys.
            terminate_backends(template_name, connection_url=self._admin_url)
            code, _, stderr = drop_db(
                template_name,
                clear_template_flag=True,
                connection_url=self._admin_url,
            )
            if code != 0:
                raise DatabaseError(
                    f"Failed to drop template {template_name}: {stderr.strip()}",
                )
            terminate_backends(cfg.db_name, connection_url=self._admin_url)
            code, _, stderr = create_db(
                template_name, template=cfg.db_name, connection_url=self._admin_url
            )
            if code != 0:  # pragma: no cover
                raise DatabaseError(
                    f"Failed to create template {template_name}: {stderr.strip()}",
                )
            log.info("Created rollback template %s", template_name)

        # Step 9: Migrate up
        t_migrate = time.monotonic()
        result = migrate_up(
            confiture_config,
            migrations_dir=migrations_dir,
            database_url=database_url,
            hooks_config=hooks_config,
        )
        migration_secs = time.monotonic() - t_migrate
        log.info(
            "Applied %d migrations (%dms)",
            result.steps_applied,
            int(migration_secs * 1000),
        )

        # Step 10: Validate table count
        if cfg.min_tables > 0:
            ok, count = validate_table_count(
                cfg.db_name,
                min_threshold=cfg.min_tables,
                connection_url=self._admin_url,
            )
            if not ok:
                raise DatabaseError(
                    f"Table count validation failed: {count} < {cfg.min_tables}",
                )
            log.info("Table count validation passed: %d >= %d", count, cfg.min_tables)

        # Step 11: Start service
        if self._service_manager and self._service_name:
            try:
                self._service_manager.start(self._service_name)
            except Exception as exc:
                raise DatabaseError(
                    f"Failed to start service {self._service_name}: {exc}"
                ) from exc
            log.info("Started service %s", self._service_name)

        total_secs = time.monotonic() - t_total
        log.info("Restore pipeline total: %dms", int(total_secs * 1000))

        # Record Prometheus metrics
        from fraisier.metrics import DeploymentMetrics

        DeploymentMetrics.restore_duration_seconds.labels(phase="pg_restore").observe(
            restore_secs
        )
        DeploymentMetrics.restore_duration_seconds.labels(phase="migration").observe(
            migration_secs
        )
        DeploymentMetrics.restore_duration_seconds.labels(phase="total").observe(
            total_secs
        )

        return StrategyResult(
            success=True,
            migrations_applied=result.steps_applied,
            restore_duration_seconds=restore_secs,
            migration_duration_seconds=migration_secs,
            total_duration_seconds=total_secs,
        )

    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
        database_url: str | None = None,
        hooks_config: dict[str, Any] | None = None,
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
                code, _, stderr = drop_db(
                    self._config.db_name, connection_url=self._admin_url
                )
                if code != 0:  # pragma: no cover
                    return StrategyResult(
                        success=False,
                        errors=[
                            f"Failed to drop database for rollback: {stderr.strip()}"
                        ],
                    )
                terminate_backends(template_name, connection_url=self._admin_url)
                code, _, stderr = create_db(
                    self._config.db_name,
                    template=template_name,
                    connection_url=self._admin_url,
                )
                if code != 0:  # pragma: no cover
                    return StrategyResult(
                        success=False,
                        errors=[f"Template rollback failed: {stderr.strip()}"],
                    )
                return StrategyResult(success=True)

            tmpl_result = reset_from_template(
                self._config.db_name,
                prefix=prefix,
                connection_url=self._admin_url,
            )
            if not tmpl_result.success:  # pragma: no cover
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
            hooks_config=hooks_config,
        )
        return StrategyResult(
            success=result.success,
            migrations_applied=result.steps_applied,
            errors=result.errors,
        )

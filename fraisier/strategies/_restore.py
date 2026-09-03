"""Staging strategy: full backup restore lifecycle, then migrate up."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fraisier.config.schema import PreflightConfig
from fraisier.dbops.confiture import migrate_down, migrate_up

from ._base import Strategy, StrategyResult

if TYPE_CHECKING:
    from fraisier.dbops.receipt import ActuationCheck

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
        project_dir: Path | None = None,
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
        # The directory the migrate step runs in (#371). ``fraisier db restore``
        # runs from wherever the operator invoked it, which is not the project.
        self._project_dir = project_dir

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
                result.migrations_checked or len(result.migrations),
                result.total_ms,
            )
        else:
            failures = "\n".join(
                f"  - {m.version} ({m.name}): {m.error}" for m in result.failures
            )
            from fraisier.errors import RECOVERY_HINTS

            message = (
                f"Migration preflight failed ({result.failure_count} of "
                f"{len(result.migrations)} migrations would fail):\n{failures}"
            )
            note = result.false_positive_note
            if note:
                # A later migration failed only because an earlier
                # non-transactional one was skipped — surface the escape hatch
                # rather than reading as a hard, mysterious block.
                message += f"\n\nNote: {note}"
                hint = RECOVERY_HINTS["migration_preflight_false_positive"]
            else:
                hint = RECOVERY_HINTS["migration_preflight"]

            raise MigrationPreflightError(
                message,
                preflight_result=result,
                recovery_hint=hint,
            )

    def _record_actuation(
        self,
        backup_file: Path,
        run_id: str,
        floor_schema: str | None = None,
    ) -> ActuationCheck:
        """Leave *run_id* in the restored database, then read it back.

        The token is what makes this a check rather than a formality: a restore
        that never ran leaves the *previous* run's receipt in place, so the
        presence of a receipt matches every time and proves nothing. Only a
        receipt naming the run that is asking can distinguish "this pipeline
        rewrote the database" from "some pipeline once did" (#358).

        Reading it back is a round trip through the database rather than trust
        in a variable this process just set.

        *floor_schema* is the schema this run derived its table-count floor for.
        It is recorded because here is the only place it is known: it comes off
        the archive's table of contents, and ``fraisier db receipt`` — which
        cross-checks relation mtimes the next morning — has no archive and no
        configuration key to learn it from. ``None`` when the archive stated no
        floor; the reader falls back rather than being told a guess.

        Never raises, and never fails the restore. It runs after every check the
        restore actually has, so a failure here is bookkeeping that did not
        happen — reported as UNVERIFIABLE, which is *not proven* rather than
        *proven bad*, exactly as an unverifiable archive is at step 2.4.
        """
        from fraisier.dbops.receipt import (
            ActuationCheck,
            ActuationVerdict,
            RestoreReceipt,
            verify_actuation,
            write_receipt,
        )

        try:
            backup_bytes = backup_file.stat().st_size
        except OSError:
            # The archive was readable minutes ago; if it is not now, that is
            # worth recording as unknown rather than guessing a size.
            backup_bytes = 0

        receipt = RestoreReceipt(
            run_id=run_id,
            backup_path=str(backup_file),
            backup_bytes=backup_bytes,
            restored_at=datetime.now(UTC),
            age_seconds=0.0,
            floor_schema=floor_schema,
        )
        failure = write_receipt(
            self._config.db_name,
            connection_url=self._admin_url,
            receipt=receipt,
        )
        if failure is not None:
            log.warning("Could not record the restore receipt: %s", failure)
            return ActuationCheck(ActuationVerdict.UNVERIFIABLE, failure)

        check = verify_actuation(
            self._config.db_name,
            connection_url=self._admin_url,
            expected_run_id=run_id,
        )
        if not check.is_actuated:
            log.warning("Restore receipt not confirmed: %s", check.detail)
        return check

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
        from fraisier.dbops.archive import ArchiveVerdict, verify_archive
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

        # Minted before anything happens, so the token belongs to this run and
        # to no other. It is written into the restored database at the end and
        # read back from it; a pipeline that never ran leaves the previous
        # run's token behind, which is the only way to tell a stale staging
        # database from a fresh one whose counts happen to match (#358).
        run_id = uuid.uuid4().hex

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

        # Step 2.4: Prove the archive is readable before anything destructive
        # happens (#343). Steps 1 and 2 look like validation and are not —
        # find_latest_backup sorts by mtime and validate_backup_age compares
        # mtime to a cutoff, so neither opens the file. Until this check, the
        # first real read was step 6, three steps after the database was
        # dropped: a dump pg_restore rejects in a second cost the staging
        # database it was meant to replace, which is the #339 incident.
        #
        # Deliberately outside both preflight conditions. --skip-preflight
        # exists for emergency restores and preflight can be disabled outright;
        # an emergency restore may skip *migration* validation, but not "is this
        # a file pg_restore can read", because that is what protects the
        # database this is about to drop. It also runs *before* preflight, whose
        # extract_schema_only would otherwise fail on the same file with a
        # murkier message.
        #
        # UNVERIFIABLE is not a bad dump — a host without the PostgreSQL client
        # tools cannot check, and must not lose the ability to restore because
        # of it. Warn and continue; is_bad is INVALID-only for this reason.
        check = verify_archive(backup_file)
        if check.is_bad:
            raise DatabaseError(
                f"Backup {backup_file} is not a readable archive: {check.detail}",
            )
        if check.verdict is ArchiveVerdict.UNVERIFIABLE:
            log.warning(
                "Could not verify %s before restoring: %s", backup_file, check.detail
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

        # The archive states the floor it can satisfy, so nobody has to invent
        # a number (#343). confiture's pre-migration counter is the instrument:
        # `pg_class WHERE relkind='r'` in a parameterised schema, which is
        # apples-to-apples with the TOC's TABLE DATA entries. Pre-migration is
        # the right checkpoint too — the TOC describes the archive, so the
        # database that must satisfy it is the one before `migrate up`; applied
        # after, any migration that drops or renames a table false-fails.
        #
        # None means the archive stated nothing — UNVERIFIABLE, or a
        # --schema-only dump with no TABLE DATA entries. That falls back to the
        # operator's floor and is reported as unchecked, never as a floor of 0.
        derived = check.schema_floor
        if derived is not None:
            floor_schema, floor_tables = derived
        else:
            floor_schema, floor_tables = "public", cfg.min_tables

        # Step 6 + 7: pg_restore (with optional ownership fix)
        t_total = time.monotonic()
        restore_result = restore_backup(
            backup_path=str(backup_file),
            db_name=cfg.db_name,
            db_owner=cfg.target_owner,
            connection_url=self._admin_url,
            jobs=cfg.jobs,
            min_tables=floor_tables,
            min_tables_schema=floor_schema,
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
        # Surface confiture's deferred-matview accounting (#172) so the deploy
        # log shows when a matview refresh was held past ANALYZE. None means the
        # backup carried no materialized views (classic three-phase restore).
        if restore_result.matviews_deferred is not None:
            log.info(
                "Deferred %d matview refresh(es) past ANALYZE (analyze_ran=%s), "
                "refreshed %s on real statistics",
                restore_result.matviews_deferred,
                restore_result.analyze_ran,
                restore_result.matviews_refreshed,
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
            project_dir=self._project_dir,
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
        elif derived is not None:
            log.info(
                "No operator floor configured (restore.min_tables); the "
                "archive's own floor of %d base table(s) in schema %s was "
                "enforced before migrations",
                derived[1],
                derived[0],
            )
        else:
            # Said, not assumed (#343). The absence of a floor used to be
            # covered by a comment claiming this step enforced one. An operator
            # reading "Restore complete" should learn whether anything counted.
            log.info(
                "No table-count floor configured (restore.min_tables) and the "
                "archive stated none; the restored database was not checked "
                "for emptiness"
            )

        # Step 10.5: Leave this run's receipt in the database it just rewrote.
        #
        # After every check, not before: a receipt written earlier would name a
        # run that had not finished, and a migration or floor failure would
        # leave the database asserting an outcome that never happened. A receipt
        # therefore means "this run completed", which is what a later caller
        # wants to know.
        #
        # The rollback template is taken before `migrate up`, so it carries no
        # receipt; a database rolled back onto it reads as MISSING. That is
        # correct — after a rollback it is not the state any completed run
        # produced, and MISSING says "not proven" rather than "stale".
        #
        # `derived[0]`, not `floor_schema`: the fallback above names `public` so
        # the floor has somewhere to count, but recording that would be
        # indistinguishable from an archive that actually stated `public`. Only
        # what the archive said is recorded.
        actuation = self._record_actuation(
            backup_file, run_id, derived[0] if derived else None
        )

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
            schema_floor=derived,
            unchecked_schemas=check.unchecked_schemas,
            actuation=actuation,
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
            project_dir=self._project_dir,
        )
        return StrategyResult(
            success=result.success,
            migrations_applied=result.steps_applied,
            errors=result.errors,
        )

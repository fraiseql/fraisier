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
from ._restore_sources import DumpSource

if TYPE_CHECKING:
    from fraisier.dbops.receipt import ActuationCheck

    from ._restore_sources import RestoreSource

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
    #: ``post_migrate_check.on_empty``: what a TVIEW that is empty over a
    #: populated view costs once the pipeline has finished rewriting the database.
    on_empty_tview: str = "fail"


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
        source: RestoreSource | None = None,
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
        # Where the data comes from (#424). The dump is the original source and
        # the default: a config that names none restores exactly as it always did.
        self._source: RestoreSource = source or DumpSource(config)

    @property
    def _resolved_template_name(self) -> str:
        return self._config.template_name or f"template_{self._config.db_name}"

    def _check_tviews_not_empty(self) -> None:
        """Refuse to go on while a pg_tviews TVIEW is empty under a full view (#422).

        ``restore_backup`` already rebuilt what a restore empties, so on a dump
        this normally finds nothing.  What it still catches is a LOGGED TVIEW
        (``only_empty`` rebuilds UNLOGGED ones) and a migration that left one
        empty.  It runs while the service is still stopped, so failing here
        starts nothing on an empty read model.

        Raises:
            DatabaseError: a TVIEW is empty, or the probe could not run, and
                ``on_empty_tview`` is ``fail``.
        """
        import psycopg

        from fraisier.dbops import tviews
        from fraisier.dbops._url import replace_db_name
        from fraisier.errors import DatabaseError

        url = replace_db_name(self._admin_url, self._config.db_name)
        try:
            empty = tviews.find_empty_tviews(url)
        except (tviews.TviewError, psycopg.Error) as exc:
            message = f"could not check TVIEWs for emptiness: {exc}"
            if self._config.on_empty_tview == "warn":
                log.warning("%s", message)
                return
            raise DatabaseError(message) from exc
        if not empty:
            return
        pairs = ", ".join(f"{e.tview} (view {e.view})" for e in empty)
        message = (
            f"{len(empty)} pg_tviews TVIEW(s) are empty while their backing view "
            f"has rows: {pairs}. Rebuild them with `fraisier db tviews rebuild "
            f"--all` before starting the service"
        )
        if self._config.on_empty_tview == "warn":
            log.warning("%s", message)
            return
        raise DatabaseError(message)

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
        """:meth:`_record_actuation_of` for a dump archive on disk."""
        from ._restore_sources import _size_or_zero

        return self._record_actuation_of(
            str(backup_file), _size_or_zero(backup_file), run_id, floor_schema
        )

    def _record_actuation_of(
        self,
        backup_ref: str,
        backup_bytes: int,
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

        *backup_ref* is what the receipt names as the source: the archive's path
        for a dump, ``pgbackrest:<stanza>/<label>`` for a physical restore (#424).

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

        receipt = RestoreReceipt(
            run_id=run_id,
            backup_path=backup_ref,
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
        allow_destructive: bool = False,
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
        from fraisier.dbops.restore import validate_table_count
        from fraisier.errors import DatabaseError

        cfg = self._config

        # Minted before anything happens, so the token belongs to this run and
        # to no other. It is written into the restored database at the end and
        # read back from it; a pipeline that never ran leaves the previous
        # run's token behind, which is the only way to tell a stale staging
        # database from a fresh one whose counts happen to match (#358).
        run_id = uuid.uuid4().hex

        # Steps 1-2.5: everything that can refuse before anything is destroyed.
        prepared = self._source.prepare(
            self,
            confiture_config=confiture_config,
            migrations_dir=migrations_dir,
            skip_preflight=skip_preflight,
        )

        # Steps 3-7: the destructive part, ending with the data back and the
        # service still stopped.
        restored = self._source.restore(self, prepared)
        t_total = restored.started_at
        restore_secs = restored.restore_secs
        derived = restored.schema_floor

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
        # `allow_destructive` is threaded; `allow_irreversible` deliberately is
        # not. This path has always migrated without `require_reversible`, and
        # starting to honour it here would newly refuse deploys that work today
        # — a separate decision from #398, which only *adds* a way to say yes.
        result = migrate_up(
            confiture_config,
            migrations_dir=migrations_dir,
            allow_destructive=allow_destructive,
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

        # Step 10.4: No TVIEW may be empty under a populated view (#422). After
        # every step that can change the database, before the receipt (which
        # means "this run completed") and before the service starts.
        self._check_tviews_not_empty()

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
        actuation = self._record_actuation_of(
            prepared.backup_ref,
            prepared.backup_bytes,
            run_id,
            derived[0] if derived else None,
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

        for phase, seconds in restored.phases.items():
            DeploymentMetrics.restore_duration_seconds.labels(phase=phase).observe(
                seconds
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
            unchecked_schemas=restored.unchecked_schemas,
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

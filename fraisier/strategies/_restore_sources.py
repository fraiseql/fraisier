"""Where a restore gets its data from (#424).

``RestoreMigrateStrategy`` used to be one function that found a ``pg_dump``
archive, proved it readable, stopped the service, dropped the database and ran
``pg_restore``.  A second source — a pgBackRest physical restore — has a
different non-destructive phase (there is no archive to open) and a different
destructive one (a cluster, not a database), so the two are separated here.

A source has two phases, and the line between them is the point at which
something is destroyed:

``prepare``
    Everything that can refuse **before** anything is touched: finding the
    backup, its age, whether it is readable, the migration preflight.

``restore``
    The destructive part, ending when the data is back and the service is still
    stopped.

The strategy keeps owning everything after that — the TVIEW report, the rollback
template, ``migrate up``, the floor check, the actuation record and the service
start — so both sources run the identical post-restore chain, and the deployment
lock the callers take covers both because the sources run inside the strategy.

Every import of a dbops function is inside the method that uses it, as it was
when this was one function: the existing tests patch those functions at their
defining module, and that only reaches a name looked up at call time.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from fraisier.dbops.archive import ArchiveCheck

    from ._restore import RestoreConfig, RestoreMigrateStrategy

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedSource:
    """What ``prepare`` established, handed to ``restore``."""

    #: What the receipt names, and what the log calls "the backup".
    backup_ref: str
    backup_bytes: int
    #: The archive check, for a dump; ``None`` for a source with no archive.
    archive_check: ArchiveCheck | None = None
    #: The archive itself, for a dump.
    backup_file: Path | None = None


@dataclass(frozen=True)
class SourceRestored:
    """What ``restore`` produced, read by the strategy's post-restore chain."""

    #: Monotonic stamp for the strategy's ``total`` timer, taken where the
    #: dump path has always started it: after the service is stopped and the
    #: database recreated, just before the data is loaded.
    started_at: float
    restore_secs: float
    #: ``(schema, table_count)`` the source says the restored database must
    #: satisfy; ``None`` when it stated none.
    schema_floor: tuple[str, int] | None
    unchecked_schemas: tuple[str, ...] = ()
    #: Seconds per phase, keyed by the ``restore_duration_seconds`` label.
    phases: dict[str, float] = field(default_factory=dict)


class RestoreSource(Protocol):
    """A way to put production's data into the staging database."""

    def prepare(
        self,
        strategy: RestoreMigrateStrategy,
        *,
        confiture_config: Path,
        migrations_dir: Path,
        skip_preflight: bool,
    ) -> PreparedSource: ...

    def restore(
        self, strategy: RestoreMigrateStrategy, prepared: PreparedSource
    ) -> SourceRestored: ...


def _stop_service(strategy: RestoreMigrateStrategy) -> None:
    """Stop the app service so nothing reconnects mid-restore."""
    from fraisier.errors import DatabaseError

    if strategy._service_manager and strategy._service_name:
        try:
            strategy._service_manager.stop(strategy._service_name)
            strategy._service_manager.wait_stopped(strategy._service_name)
        except Exception as exc:
            raise DatabaseError(
                f"Failed to stop service {strategy._service_name}: {exc}"
            ) from exc
        log.info("Stopped service %s", strategy._service_name)


class DumpSource:
    """A ``pg_dump`` archive in ``restore.backup_dir`` — the original source."""

    def __init__(self, config: RestoreConfig) -> None:
        self._config = config

    def prepare(
        self,
        strategy: RestoreMigrateStrategy,
        *,
        confiture_config: Path,
        migrations_dir: Path,
        skip_preflight: bool,
    ) -> PreparedSource:
        from fraisier.dbops.archive import ArchiveVerdict, verify_archive
        from fraisier.dbops.restore import find_latest_backup, validate_backup_age
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
        if not skip_preflight and strategy._preflight_enabled():
            strategy._run_preflight(
                backup_path=backup_file,
                confiture_config=confiture_config,
                migrations_dir=migrations_dir,
            )

        return PreparedSource(
            backup_ref=str(backup_file),
            backup_bytes=_size_or_zero(backup_file),
            archive_check=check,
            backup_file=backup_file,
        )

    def restore(
        self, strategy: RestoreMigrateStrategy, prepared: PreparedSource
    ) -> SourceRestored:
        from fraisier.dbops.operations import create_db, drop_db, terminate_backends
        from fraisier.dbops.restore import restore_backup
        from fraisier.errors import DatabaseError

        cfg = self._config
        admin_url = strategy._admin_url
        check = prepared.archive_check
        backup_file = prepared.backup_file
        assert check is not None
        assert backup_file is not None

        # Step 3: Stop service to prevent connection reconnect race
        _stop_service(strategy)

        # Step 4: Terminate connections
        terminate_backends(cfg.db_name, connection_url=admin_url)
        log.info("Terminated connections to %s", cfg.db_name)

        # Step 5: Drop and recreate database
        code, _, stderr = drop_db(cfg.db_name, force=True, connection_url=admin_url)
        if code != 0:
            raise DatabaseError(
                f"Failed to drop database {cfg.db_name}: {stderr.strip()}",
            )
        code, _, stderr = create_db(cfg.db_name, connection_url=admin_url)
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
        started_at = time.monotonic()
        restore_result = restore_backup(
            backup_path=str(backup_file),
            db_name=cfg.db_name,
            db_owner=cfg.target_owner,
            connection_url=admin_url,
            jobs=cfg.jobs,
            min_tables=floor_tables,
            min_tables_schema=floor_schema,
        )
        restore_secs = restore_result.duration_seconds
        if not restore_result.success:
            # The message names the step that failed. It used to say
            # "pg_restore failed" for an ownership reassignment that runs
            # *after* a successful restore (#380).
            raise DatabaseError(restore_result.error)
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

        # Said whether or not anything was empty: a restore that rebuilt nothing
        # and one that never looked must not read alike (#422). None means the
        # database has no pg_tviews.
        if restore_result.tviews_rebuilt is not None:
            log.info(
                "pg_tviews: rebuilt %d empty TVIEW(s)%s",
                len(restore_result.tviews_rebuilt),
                "".join(
                    f" {r.entity}={r.rows}rows" for r in restore_result.tviews_rebuilt
                ),
            )

        return SourceRestored(
            started_at=started_at,
            restore_secs=restore_secs,
            schema_floor=derived,
            unchecked_schemas=check.unchecked_schemas,
            phases={"pg_restore": restore_secs},
        )


def _size_or_zero(path: Path) -> int:
    """The archive's size, or 0 when it cannot be read.

    The archive was readable minutes ago; if it is not now, that is worth
    recording as unknown rather than guessing a size.
    """
    try:
        return path.stat().st_size
    except OSError:
        return 0

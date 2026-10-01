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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from fraisier.config.restore_source import PgBackRestSpec
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
    #: The backup a physical restore will restore, by pgBackRest label (#424).
    backup_label: str | None = None


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


class PgBackRestSource:
    """A pgBackRest ``--delta`` restore of a dedicated staging cluster (#424).

    Where :class:`DumpSource` rebuilds one database from a logical archive, this
    refreshes a **whole cluster** from the physical backup production already makes
    for disaster recovery, rewriting only the files that changed.  The cluster
    stop, the restore and the start happen as root in the helper
    (:mod:`fraisier.pgbackrest_helper`); this class decides, orders and **checks**.

    ``prepare`` chooses the backup — its label, stop time and age are validated and
    logged while the cluster is still up.  ``restore`` is destructive, and has one
    rule: once the cluster has been stopped, *any* failure stops it again and raises
    :class:`~fraisier.errors.RestoreFailedClosed`, because what is on disk is a
    half-restored production copy that must be neither served nor allowed to
    archive into production's repository.
    """

    def __init__(
        self,
        config: RestoreConfig,
        *,
        client: Any = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._now = now or (lambda: datetime.now(UTC))

    def _helper(self) -> Any:
        """The helper client, built on first use."""
        if self._client is None:
            from fraisier.dbops.pgbackrest import HelperClient

            spec = self._spec()
            assert self._config.pgbackrest_socket, "pgbackrest_socket not configured"
            self._client = HelperClient(
                self._config.pgbackrest_socket,
                long_timeout=spec.timeout_seconds + 60,
            )
        return self._client

    def _spec(self) -> PgBackRestSpec:
        assert self._config.pgbackrest is not None
        return self._config.pgbackrest

    # -- choosing the backup (nothing is touched) --------------------------------

    def prepare(
        self,
        strategy: RestoreMigrateStrategy,
        *,
        confiture_config: Path,
        migrations_dir: Path,
        skip_preflight: bool,
    ) -> PreparedSource:
        from fraisier.dbops import pgbackrest
        from fraisier.errors import DatabaseError

        spec = self._spec()
        try:
            backups = pgbackrest.parse_info(self._helper().info(), stanza=spec.stanza)
            choice = pgbackrest.select_backup(backups, spec)
            status = self._helper().status()
        except (pgbackrest.HelperUnavailableError, pgbackrest.BackupChoiceError) as exc:
            raise DatabaseError(f"Cannot choose a pgBackRest backup: {exc}") from exc

        # While the cluster is still up, prove that `admin_url` reaches the cluster
        # the helper will restore, before anything is stopped. A stopped cluster (the
        # re-run after a fail-closed refresh) cannot be asked, and is checked after
        # the restore instead, where it is mandatory.
        if status.get("online"):
            self._require_the_helpers_cluster(strategy._admin_url, status)

        age_hours = (self._now() - choice.stop).total_seconds() / 3600
        # An instant is the point of a point-in-time refresh: an old backup is not
        # a stale one there, only for `latest`.
        if spec.is_latest and age_hours > self._config.max_age_hours:
            raise DatabaseError(
                f"Backup {choice.label} stopped {age_hours:.1f}h ago, which is "
                f"older than {self._config.max_age_hours}h"
            )
        log.info(
            "pgBackRest backup %s (%s) stopped %s (%.1fh ago), %.1f MB, stanza %s "
            "repo %d -> cluster %s",
            choice.label,
            choice.kind,
            choice.stop.isoformat(),
            age_hours,
            choice.size_bytes / 1024**2,
            spec.stanza,
            spec.repo,
            spec.cluster,
        )
        if not skip_preflight and strategy._preflight_enabled():
            # Said, not silent: the migration preflight reads a pg_dump archive,
            # and a physical restore has none. A rehearsal against the restored
            # cluster is a separate piece of work.
            log.info(
                "Migration preflight skipped: it reads a pg_dump archive and a "
                "pgBackRest restore has none"
            )
        return PreparedSource(
            backup_ref=f"pgbackrest:{spec.stanza}/{choice.label}",
            backup_bytes=choice.size_bytes,
            backup_label=choice.label,
        )

    # -- destroying and rebuilding the cluster -----------------------------------

    def restore(
        self, strategy: RestoreMigrateStrategy, prepared: PreparedSource
    ) -> SourceRestored:
        from fraisier.dbops import pgbackrest
        from fraisier.errors import DatabaseError, RestoreFailedClosed

        # Before the cluster is touched: failing here leaves a running cluster and
        # a service that can simply be restarted, so these are ordinary errors.
        _stop_service(strategy)
        try:
            self._helper().stop()
        except pgbackrest.HelperUnavailableError as exc:
            raise DatabaseError(
                f"Failed to stop cluster {self._spec().cluster}: {exc}"
            ) from exc

        # From here the cluster is stopped. `BaseException`, not `Exception`: a
        # deploy's `timeout:` is delivered as an exception into whatever is running,
        # and one that escaped would reach a failed-deploy handler that restarts the
        # service against a half-restored cluster.
        try:
            started_at = time.monotonic()
            phases = self._refresh(strategy, prepared)
        except RestoreFailedClosed:
            raise
        except BaseException as exc:
            raise self._fail_closed(str(exc) or type(exc).__name__) from exc
        return SourceRestored(
            started_at=started_at,
            restore_secs=sum(phases.values()),
            schema_floor=None,  # no archive table of contents to state one
            phases=phases,
        )

    def _require_the_helpers_cluster(
        self, admin_url: str, status: dict[str, Any]
    ) -> None:
        """``admin_url`` must reach the cluster the helper restores.

        The safety checks — and ``ALTER SYSTEM RESET`` — run against whatever
        ``admin_url`` (from ``fraises.yaml``) reaches. The helper reports the data
        directory of the cluster it restores, read from ``pg_lsclusters``; the
        server behind the URL reports its own. They must be the same directory, or
        ``archive_mode`` would be read from the wrong server and a reset applied to
        it.
        """
        import posixpath

        from fraisier.dbops import pgbackrest
        from fraisier.errors import DatabaseError

        expected = status.get("datadir")
        if not expected:
            raise DatabaseError(
                "the pgBackRest helper reported no data directory, so it cannot be "
                "proven that admin_url reaches the cluster being restored"
            )
        try:
            actual = pgbackrest.read_setting(admin_url, "data_directory")
        except Exception as exc:
            raise DatabaseError(
                f"could not read the data directory behind admin_url: {exc}"
            ) from exc
        if posixpath.normpath(actual) != posixpath.normpath(str(expected)):
            raise DatabaseError(
                f"admin_url reaches a cluster whose data directory is {actual}, but "
                f"the helper restores {expected}: refusing to run checks, or a reset, "
                f"against a cluster that is not the restored one"
            )

    def _fail_closed(self, reason: str) -> Exception:
        """Stop the cluster again and build the error that keeps the service down."""
        from fraisier.dbops import pgbackrest
        from fraisier.errors import RestoreFailedClosed

        try:
            self._helper().stop()
        except pgbackrest.HelperUnavailableError as exc:
            log.critical(
                "Could not stop cluster %s after a failed restore: %s",
                self._spec().cluster,
                exc,
            )
            reason += f" (and the cluster could not be stopped again: {exc})"
        return RestoreFailedClosed(
            f"{reason}. Failed closed: the cluster was stopped and the service was "
            f"not started."
        )

    def _refresh(
        self, strategy: RestoreMigrateStrategy, prepared: PreparedSource
    ) -> dict[str, float]:
        """Restore, start, wait for promotion, verify, hand back; failures raise."""
        from fraisier.dbops import pgbackrest, tviews
        from fraisier.dbops import restore as dbrestore
        from fraisier.dbops._url import replace_db_name
        from fraisier.errors import DatabaseError

        cfg = self._config
        spec = self._spec()
        admin_url = strategy._admin_url
        client = self._helper()
        label = prepared.backup_label
        assert label is not None
        phases: dict[str, float] = {}

        def timed(phase: str, started: float) -> None:
            phases[phase] = time.monotonic() - started

        t = time.monotonic()
        reply = client.restore(label)
        timed("pgbackrest_restore", t)
        summary = reply.get("summary") or {}
        log.info(
            "pgBackRest restored %s into cluster %s in %dms: %s of %s files "
            "rewritten (~%.1f MB written; the rest already matched)",
            summary.get("label") or label,
            spec.cluster,
            int(phases["pgbackrest_restore"] * 1000),
            summary.get("files_rewritten", "?"),
            summary.get("files_total", "?"),
            (summary.get("bytes_rewritten") or 0) / 1024**2,
        )

        t = time.monotonic()
        client.start()
        timed("cluster_start", t)

        t = time.monotonic()
        pgbackrest.wait_until_promoted(admin_url, timeout_seconds=spec.timeout_seconds)
        timed("recovery", t)

        t = time.monotonic()
        status = client.status()
        # Before the first statement that changes anything.
        self._require_the_helpers_cluster(admin_url, status)
        mode = pgbackrest.read_setting(admin_url, "archive_mode")
        if mode != "off":
            raise DatabaseError(
                f"archive_mode is {mode!r} on the restored cluster, not 'off': it "
                f"would archive WAL into stanza {spec.stanza!r}, corrupting "
                f"production's disaster-recovery timeline"
            )
        leftovers = {
            name: value
            for name, value in pgbackrest.reset_replication_settings(admin_url).items()
            if value
        }
        if leftovers:
            raise DatabaseError(
                f"replication settings survived the reset on the restored cluster: "
                f"{', '.join(sorted(leftovers))}"
            )
        signals = status.get("signals")
        if not isinstance(signals, list):
            raise DatabaseError(
                f"the helper's status carried no usable 'signals' list ({signals!r}), "
                f"so the data directory cannot be shown to be clean"
            )
        if signals:
            raise DatabaseError(
                f"{', '.join(str(x) for x in signals)} still present in the restored "
                f"cluster's data directory: it would re-enter recovery on its "
                f"next start"
            )
        if not pgbackrest.database_exists(admin_url, cfg.db_name):
            raise DatabaseError(
                f"the restored cluster has no database {cfg.db_name!r}. A physical "
                f"restore carries production's databases under production's names, so "
                f"database.name must be the name inside the backup"
            )
        timed("safety", t)

        if cfg.target_owner:
            code, _, stderr = dbrestore._reassign_owner(
                cfg.db_name, cfg.target_owner, connection_url=admin_url
            )
            if code != 0:
                raise DatabaseError(
                    f"reassign_owner failed: ownership reassignment to "
                    f"{cfg.target_owner} did not run: {stderr.strip()}"
                )

        # The same chain the dump path runs, and load-bearing here: a physical
        # restore empties every UNLOGGED TVIEW (#422).
        restored_url = replace_db_name(admin_url, cfg.db_name)
        if tviews.tviews_installed(restored_url):
            rebuilt = tviews.rebuild_empty_tviews(restored_url)
            log.info(
                "pg_tviews: rebuilt %d empty TVIEW(s)%s",
                len(rebuilt),
                "".join(f" {r.entity}={r.rows}rows" for r in rebuilt),
            )
        return phases

"""Core deployment strategies: MigrateStrategy and RebuildStrategy."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fraisier.dbops._url import resolve_db_url

if TYPE_CHECKING:
    from fraisier.config._lazy_env import LazyEnv
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

from ._base import Strategy, StrategyResult

# Import Migrator for re-baseline step (optional import)
try:
    from confiture.core.migrator import Migrator
except ImportError:  # pragma: no cover
    Migrator = None  # type: ignore

log = logging.getLogger(__name__)


class MigrateStrategy(Strategy):
    """Production: preflight → optional verified dump gate → migrate up.

    When *pre_migrate_dump* is configured (``enabled: true``), a
    ``pg_dump`` of the target database is taken and verified (TOC read +
    size-sanity vs the previous dump, via :func:`fraisier.dbops.backup.
    run_backup`) **before** any migration is applied.  If the dump cannot
    be produced or fails verification, the deploy aborts with the
    migrations untouched: *no fresh verified dump ⇒ no migration*.  This
    bounds the recovery point to the moment immediately before the
    deploy instead of the last scheduled backup.

    The gate only fires when there are pending migrations — a no-op
    deploy (nothing to apply) does not spend a full dump.

    Config keys (``database.pre_migrate_dump`` in fraises.yaml):

    - ``enabled`` (bool, default false)
    - ``output_dir`` (str, required when enabled) — fast-local rollback path
    - ``compression`` (str, default ``zstd:9``)
    - ``jobs`` (int, default 1) — parallel dump workers (``-Fd -j N``)
    - ``min_free_gb`` (int, optional) — abort if *output_dir* has less free
    - ``retention_hours`` (int, optional) — prune older gate dumps in
      *output_dir* after a **successful** dump (a failed dump never
      deletes anything)
    """

    def __init__(
        self,
        *,
        pre_migrate_dump: dict[str, Any] | None = None,
        db_name: str = "",
        project_dir: Path | None = None,
    ) -> None:
        self._dump_config = pre_migrate_dump or {}
        self._db_name = db_name
        # The directory migrations run in — the same one ``db_deploy.sh`` does
        # ``cd "${PROJECT_DIR}"`` into. Held on the strategy rather than at the
        # call site so ``execute`` and ``rollback`` cannot drift apart (#371).
        self._project_dir = project_dir
        if self._dump_enabled:
            if not self._dump_config.get("output_dir"):
                msg = "pre_migrate_dump.enabled requires pre_migrate_dump.output_dir"
                raise ValueError(msg)
            if not db_name:
                msg = "pre_migrate_dump.enabled requires the database name"
                raise ValueError(msg)
            validate_pg_identifier(db_name, "database name")

    @property
    def _dump_enabled(self) -> bool:
        return bool(self._dump_config.get("enabled", False))

    def _run_dump_gate(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path,
        db_url: str | None,
    ) -> str | None:
        """Run the pre-migration dump gate.  Returns an error message to
        abort the deploy, or None to proceed."""
        from fraisier.dbops.backup import (
            check_disk_space,
            cleanup_old_backups,
            run_backup,
        )
        from fraisier.dbops.confiture import has_pending

        if not has_pending(
            confiture_config, migrations_dir=migrations_dir, database_url=db_url
        ):
            log.info("pre_migrate_dump: no pending migrations — gate skipped")
            return None

        output_dir = self._dump_config["output_dir"]
        min_free_gb = self._dump_config.get("min_free_gb")
        if min_free_gb is not None and not check_disk_space(
            output_dir, required_gb=int(min_free_gb)
        ):
            return (
                f"pre-migration dump gate: less than {min_free_gb} GB free "
                f"in {output_dir} — refusing to migrate without a fresh dump"
            )

        result = run_backup(
            db_name=self._db_name,
            output_dir=output_dir,
            database_url=db_url or "",
            compression=self._dump_config.get("compression", "zstd:9"),
            mode="full",
            jobs=int(self._dump_config.get("jobs", 1)),
        )
        if not result.success:
            return (
                "pre-migration dump gate failed — migrations NOT applied: "
                f"{result.error}"
            )

        log.info("pre_migrate_dump: verified dump at %s", result.backup_path)
        retention_hours = self._dump_config.get("retention_hours")
        if retention_hours is not None:
            # keep_minimum=1: this gate's dump is the rollback point for the
            # migration about to run. Expiring the newest one leaves that
            # migration with nothing to fall back to.
            outcome = cleanup_old_backups(
                Path(output_dir),
                retention_hours=int(retention_hours),
                keep_minimum=1,
            )
            if outcome.removed:
                log.info(
                    "pre_migrate_dump: pruned %d old dump(s)", len(outcome.removed)
                )
        return None

    def execute(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        allow_irreversible: bool = False,
        pre_migrate_verify: bool = False,
        database_url: str | LazyEnv | None = None,
        hooks_config: dict[str, Any] | None = None,
    ) -> StrategyResult:
        # Resolve LazyEnv at the strategy boundary so every downstream
        # dbops call receives a concrete `str`.
        db_url = resolve_db_url(database_url, role="database_url")
        preflight(
            confiture_config,
            migrations_dir=migrations_dir,
            allow_irreversible=allow_irreversible,
            database_url=db_url,
        )

        if self._dump_enabled:
            gate_error = self._run_dump_gate(
                confiture_config, migrations_dir=migrations_dir, db_url=db_url
            )
            if gate_error:
                return StrategyResult(success=False, errors=[gate_error])

        result = migrate_up(
            confiture_config,
            migrations_dir=migrations_dir,
            pre_migrate_verify=pre_migrate_verify,
            require_reversible=not allow_irreversible,
            database_url=db_url,
            hooks_config=hooks_config,
            project_dir=self._project_dir,
        )
        return StrategyResult(success=True, migrations_applied=result.steps_applied)

    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
        database_url: str | LazyEnv | None = None,
        hooks_config: dict[str, Any] | None = None,
    ) -> StrategyResult:
        db_url = resolve_db_url(database_url, role="database_url")
        result = migrate_down(
            confiture_config,
            migrations_dir=migrations_dir,
            steps=steps,
            database_url=db_url,
            hooks_config=hooks_config,
            project_dir=self._project_dir,
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
        admin_url: str | LazyEnv | None = None,
        create_template: bool = False,
        template_name: str | None = None,
        app_version: str | None = None,
    ) -> None:
        self._required_roles: list[str] = []
        for role in required_roles or []:
            validate_pg_identifier(role, "required role")
            self._required_roles.append(role)
        self._project_dir = project_dir
        self._admin_url = admin_url
        if template_name:
            validate_pg_identifier(template_name, "template name")
        self._create_template = create_template
        self._template_name = template_name
        if app_version:
            # Loud failure on a typo'd fraises.yaml value. Auto-discovered
            # values from version.json / pyproject.toml are validated
            # silently in resolve_app_version (warn-and-skip path).
            from fraisier.versioning import APP_VERSION_RE

            if not APP_VERSION_RE.match(app_version):
                msg = (
                    f"invalid app_version {app_version!r}: must match "
                    f"{APP_VERSION_RE.pattern}"
                )
                raise ValueError(msg)
        self._app_version = app_version

    def _resolved_template_name(self, db_name: str) -> str:
        """Return the template DB name: explicit or derived from db_name."""
        return self._template_name or f"template_{db_name}"

    def _stamp_source_for_template(self, db_name: str, connection_url: str) -> None:
        """Best-effort: stamp the live DB so the template carries app_version.

        Failures (no resolvable version, missing/empty ``tb_version``, psql
        error) log at WARNING and never raise — the consumer side is
        responsible for rejecting unstamped/stale templates.
        """
        from fraisier.versioning import APP_VERSION_RE, resolve_app_version

        resolved = resolve_app_version(self._project_dir, override=self._app_version)
        if resolved is None:
            log.warning(
                "Skipping pre-clone version stamp on %s: no app version "
                "could be resolved (override=%r, project_dir=%s); "
                "template will be unstamped",
                db_name,
                self._app_version,
                self._project_dir,
            )
            return
        version, source = resolved

        if not APP_VERSION_RE.match(version):  # pragma: no cover
            log.warning(
                "Refusing to stamp %s: resolved version %r failed "
                "interpolation re-validation",
                db_name,
                version,
            )
            return

        sql = f"UPDATE public.tb_version SET app_version = '{version}'"
        code, stdout, stderr = run_psql(
            sql, db_name=db_name, connection_url=connection_url
        )
        if code != 0:
            log.warning(
                "Could not stamp %s with app_version=%s (from %s): %s",
                db_name,
                version,
                source,
                stderr.strip(),
            )
            return
        if "UPDATE 0" in stdout:
            log.warning(
                "tb_version on %s is empty; template will be unstamped. "
                "Project must seed tb_version with at least one row for "
                "the stamp to take effect.",
                db_name,
            )
            return
        log.info(
            "Stamped %s with app_version=%s (from %s) before template clone",
            db_name,
            version,
            source,
        )

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
        connection_url: str,
    ) -> None:
        """Ensure required roles exist and are granted to the db owner."""
        # Defense-in-depth: re-validate every identifier interpolated into the
        # SQL strings below. ``run_psql`` shells out to ``psql -c`` so we cannot
        # use parameter binding for identifiers; the regex in
        # ``validate_pg_identifier`` rejects single quotes, semicolons,
        # whitespace, and shell metacharacters, which covers both the
        # bare-identifier vector (``CREATE ROLE {role}``) and the
        # single-quoted-literal vector (``rolname = '{role}'``) below.
        if db_owner is not None:
            validate_pg_identifier(db_owner, "database owner")
        for role in self._required_roles:
            validate_pg_identifier(role, "required role")
            sql = (
                "DO $$ BEGIN "
                f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') "
                f"THEN CREATE ROLE {role} NOLOGIN; END IF; END $$;"
            )
            code, _, stderr = run_psql(
                sql, db_name=db_name, connection_url=connection_url
            )
            if code != 0:  # pragma: no cover
                raise subprocess.CalledProcessError(code, "psql", stderr=stderr)
            log.info("Ensured role %s exists", role)

            if db_owner:
                grant_sql = f"GRANT {role} TO {db_owner}"
                code, _, stderr = run_psql(
                    grant_sql,
                    db_name=db_name,
                    connection_url=connection_url,
                )
                if code != 0:  # pragma: no cover
                    raise subprocess.CalledProcessError(code, "psql", stderr=stderr)
                log.info("Granted %s to %s", role, db_owner)

    def execute(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        allow_irreversible: bool = False,
        pre_migrate_verify: bool = False,
        database_url: str | LazyEnv | None = None,
        hooks_config: dict[str, Any] | None = None,
    ) -> StrategyResult:
        import tempfile
        from urllib.parse import urlparse

        import yaml
        from confiture.config.environment import Environment
        from confiture.core.builder import SchemaBuilder

        # Resolve LazyEnv inputs at the strategy boundary so pydantic
        # validation of the confiture Environment and every downstream
        # dbops call receives concrete `str` URLs.
        db_url = resolve_db_url(database_url, role="database_url")

        # Load environment from config YAML.
        raw: dict = yaml.safe_load(  # type: ignore[assignment]
            Path(confiture_config).read_text()
        )
        if db_url:
            raw["database_url"] = db_url
        env = Environment.model_validate(raw)

        # Parse database name and owner from the connection URL.
        parsed = urlparse(env.database_url)
        db_name = parsed.path.lstrip("/")
        db_owner = parsed.username

        # Admin URL for privileged operations (DROP/CREATE DATABASE).
        # Priority: explicit admin_url > derived from database_url.
        from fraisier.dbops._url import replace_db_name

        admin_url: str | None = resolve_db_url(self._admin_url, role="admin_url")
        if not admin_url:
            admin_url = replace_db_name(env.database_url, "postgres")
        if not admin_url:  # pragma: no cover - defensive
            msg = (
                "RebuildStrategy requires admin_url (or a database_url from "
                "which one can be derived)"
            )
            raise ValueError(msg)

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
            if code != 0:  # pragma: no cover
                raise subprocess.CalledProcessError(code, "createdb", stderr=stderr)

            # Provision required roles before schema apply so that
            # CREATE SCHEMA … AUTHORIZATION <role> doesn't fail.
            if self._required_roles:
                self._provision_roles(db_name, db_owner, connection_url=admin_url)

            # Phase 1: Apply superuser SQL (roles, extensions) via admin_url.
            # Compute the admin connection URL targeting the app database.
            # Superuser SQL must land in the app db, not postgres.
            admin_app_conn = replace_db_name(admin_url, db_name)

            # Phase 1: Apply superuser pre-schema SQL (roles, extensions).
            if split.superuser_pre_files > 0:
                self._apply_sql(admin_app_conn, superuser_pre_path)

            # Phase 2: Apply app SQL (schemas, tables, views, data).
            self._apply_sql(env.database_url, app_path)

            # Phase 3: Apply superuser post-schema SQL (grants on tables,
            # role settings) — requires tables to exist, hence after app.
            if split.superuser_post_files > 0:
                self._apply_sql(admin_app_conn, Path(split.superuser_post_path))
        finally:
            import shutil

            shutil.rmtree(output_dir, ignore_errors=True)

        # Re-baseline migration tracking table.
        with Migrator.from_config(env, migrations_dir=migrations_dir) as m:
            m.reinit()

        # Snapshot the freshly-built DB as a template for fast reseeds.
        if self._create_template:
            template_name = self._resolved_template_name(db_name)
            terminate_backends(template_name, connection_url=admin_url)
            # clear_template_flag: Postgres refuses to drop a database with
            # datistemplate=true (even WITH FORCE); fixes #200 re-deploys.
            code, _, stderr = drop_db(
                template_name,
                clear_template_flag=True,
                connection_url=admin_url,
            )
            if code != 0:
                raise subprocess.CalledProcessError(code, "dropdb", stderr=stderr)
            # Kick any reconnected app off before stamping so the stamp is
            # the last write to tb_version before the clone.
            terminate_backends(db_name, connection_url=admin_url)
            self._stamp_source_for_template(db_name, connection_url=env.database_url)
            # Belt-and-suspenders: catch anything that reconnected between
            # the stamp's psql exit and the clone. CREATE DATABASE … TEMPLATE …
            # itself fails closed if any backend is still attached.
            terminate_backends(db_name, connection_url=admin_url)
            code, _, stderr = create_db(
                template_name, template=db_name, connection_url=admin_url
            )
            if code != 0:
                raise subprocess.CalledProcessError(code, "createdb", stderr=stderr)
            log.info("Created template database %s from %s", template_name, db_name)

        return StrategyResult(success=True)

    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
        database_url: str | None = None,
        hooks_config: dict[str, Any] | None = None,
    ) -> StrategyResult:
        return self.execute(
            confiture_config, migrations_dir=migrations_dir, database_url=database_url
        )

"""Core deployment strategies: MigrateStrategy and RebuildStrategy."""

from __future__ import annotations

import logging
import subprocess
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

from ._base import Strategy, StrategyResult

# Import Migrator for re-baseline step (optional import)
try:
    from confiture.core.migrator import Migrator
except ImportError:  # pragma: no cover
    Migrator = None  # type: ignore

log = logging.getLogger(__name__)


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
        hooks_config: dict[str, Any] | None = None,
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
            hooks_config=hooks_config,
        )
        return StrategyResult(success=True, migrations_applied=result.steps_applied)

    def rollback(
        self,
        confiture_config: Path,
        *,
        migrations_dir: Path = Path("db/migrations"),
        steps: int,
        database_url: str | None = None,
        hooks_config: dict[str, Any] | None = None,
    ) -> StrategyResult:
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
        self._app_version = app_version

    def _resolved_template_name(self, db_name: str) -> str:
        """Return the template DB name: explicit or derived from db_name."""
        return self._template_name or f"template_{db_name}"

    def _stamp_source_for_template(
        self, db_name: str, connection_url: str
    ) -> None:
        """Best-effort: stamp the live DB so the template carries app_version.

        Failures (no resolvable version, missing/empty ``tb_version``, psql
        error) log at WARNING and never raise — the consumer side is
        responsible for rejecting unstamped/stale templates.
        """
        from fraisier.versioning import APP_VERSION_RE, resolve_app_version

        resolved = resolve_app_version(
            self._project_dir, override=self._app_version
        )
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
        database_url: str | None = None,
        hooks_config: dict[str, Any] | None = None,
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
        # Priority: explicit admin_url > derived from database_url.
        from fraisier.dbops._url import replace_db_name

        admin_url: str | None = self._admin_url
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
            drop_db(template_name, connection_url=admin_url)
            # Kick any reconnected app off before stamping so the stamp is
            # the last write to tb_version before the clone.
            terminate_backends(db_name, connection_url=admin_url)
            self._stamp_source_for_template(
                db_name, connection_url=env.database_url
            )
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

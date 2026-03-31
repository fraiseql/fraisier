"""Template database lifecycle manager.

Orchestrates the create/clone/rebuild/clean lifecycle for test database
templates, using confiture's SchemaBuilder for schema builds and hash
computation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fraisier.dbops._url import replace_db_name
from fraisier.dbops.operations import (
    check_db_exists,
    create_db,
    terminate_backends,
)
from fraisier.dbops.templates import cleanup_templates, create_template
from fraisier.testing._metadata import (
    ensure_meta_table,
    read_meta,
    write_meta,
)
from fraisier.testing._timing import TimingReport, timed_phase

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


log = logging.getLogger("fraisier.testing")


@dataclass
class TemplateInfo:
    """Result of ensure_template."""

    template_name: str
    schema_hash: str
    from_cache: bool
    timing: TimingReport | None = None


@dataclass
class TemplateStatus:
    """Current state of the template database."""

    template_name: str
    template_exists: bool
    current_hash: str
    stored_hash: str | None
    needs_rebuild: bool
    built_at: datetime | None = None
    build_duration_ms: int | None = None


class TemplateManager:
    """Orchestrates test database template lifecycle.

    Uses confiture's SchemaBuilder for schema builds and hash computation,
    and fraisier's dbops primitives for PostgreSQL operations.
    """

    def __init__(
        self,
        env: str,
        *,
        project_dir: Path,
        confiture_config: Path,
        connection_url: str | None = None,
        sudo_user: str = "postgres",
        template_prefix: str = "tpl_test_",
    ) -> None:
        self._env = env
        self._project_dir = project_dir
        self._confiture_config = confiture_config
        self._connection_url = connection_url
        self._sudo_user = sudo_user
        self._template_prefix = template_prefix

    @property
    def template_name(self) -> str:
        return f"{self._template_prefix}{self._env}"

    def _compute_hash(self) -> str:
        """Compute schema hash via confiture's SchemaBuilder."""
        from confiture.core.builder import SchemaBuilder

        builder = SchemaBuilder(env=self._env, project_dir=self._project_dir)
        return builder.compute_hash()

    def _db_name_from_url(self) -> str:
        """Extract database name from connection URL."""
        if not self._connection_url:
            msg = "connection_url required to derive database name"
            raise ValueError(msg)
        parsed = urlparse(self._connection_url)
        return parsed.path.lstrip("/")

    def _admin_url(self) -> str | None:
        """Derive admin URL (pointing to 'postgres' db) from connection URL."""
        if not self._connection_url:
            return None
        return replace_db_name(self._connection_url, "postgres")

    def _template_url(self) -> str | None:
        """Connection URL pointing to the template database."""
        if not self._connection_url:
            return None
        return replace_db_name(self._connection_url, self.template_name)

    def ensure_template(self) -> TemplateInfo:
        """Ensure a valid template database exists.

        Computes schema hash, checks for existing template, and rebuilds
        only when needed. Returns template info with cache status.
        """
        report = TimingReport()

        with timed_phase("hash_check", log) as elapsed:
            current_hash = self._compute_hash()
        report.record("hash_check", elapsed.ms)

        with timed_phase("template_check", log) as elapsed:
            exists = check_db_exists(
                self.template_name,
                sudo_user=self._sudo_user,
                connection_url=self._admin_url(),
            )
        report.record("template_check", elapsed.ms)

        if exists:
            meta = read_meta(
                self.template_name,
                connection_url=self._template_url(),
                sudo_user=self._sudo_user,
            )
            if meta and meta.schema_hash == current_hash:
                log.info(
                    "Template %s up to date (hash %s)",
                    self.template_name,
                    current_hash[:12],
                )
                return TemplateInfo(
                    template_name=self.template_name,
                    schema_hash=current_hash,
                    from_cache=True,
                    timing=report,
                )
            log.info(
                "Template %s stale (stored=%s, current=%s), rebuilding",
                self.template_name,
                meta.schema_hash[:12] if meta else "none",
                current_hash[:12],
            )

        return self.build_template(_report=report)

    def build_template(self, *, _report: TimingReport | None = None) -> TemplateInfo:
        """Force rebuild the template database.

        Builds schema via RebuildStrategy, snapshots as template,
        records metadata.
        """
        from fraisier.strategies import RebuildStrategy

        report = _report or TimingReport()

        with timed_phase("schema_build", log) as elapsed:
            strategy = RebuildStrategy(
                project_dir=self._project_dir,
                admin_url=self._admin_url(),
            )
            strategy.execute(
                self._confiture_config,
                database_url=self._connection_url,
            )
        build_ms = elapsed.ms
        report.record("schema_build", build_ms)

        with timed_phase("template_create", log) as elapsed:
            db_name = self._db_name_from_url()
            result = create_template(
                db_name,
                prefix=self._template_prefix,
                sudo_user=self._sudo_user,
                connection_url=self._admin_url(),
            )
            if not result.success:
                msg = f"Failed to create template: {result.error}"
                raise RuntimeError(msg)
        report.record("template_create", elapsed.ms)

        with timed_phase("metadata_write", log) as elapsed:
            current_hash = self._compute_hash()

            ensure_meta_table(
                self.template_name,
                connection_url=self._template_url(),
                sudo_user=self._sudo_user,
            )

            from datetime import UTC, datetime

            from fraisier.testing._metadata import TemplateMeta

            meta = TemplateMeta(
                schema_hash=current_hash,
                built_at=datetime.now(tz=UTC),
                confiture_version=_confiture_version(),
                build_duration_ms=build_ms,
            )
            write_meta(
                self.template_name,
                meta,
                connection_url=self._template_url(),
                sudo_user=self._sudo_user,
            )
        report.record("metadata_write", elapsed.ms)

        log.info("Template %s built\n%s", self.template_name, report.summary())

        return TemplateInfo(
            template_name=self.template_name,
            schema_hash=current_hash,
            from_cache=False,
            timing=report,
        )

    def clone(self, test_db_name: str) -> str:
        """Clone the template into a new test database.

        Returns the connection URL for the cloned database.
        """
        admin_url = self._admin_url()

        terminate_backends(
            self.template_name,
            sudo_user=self._sudo_user,
            connection_url=admin_url,
        )

        code, _, stderr = create_db(
            test_db_name,
            template=self.template_name,
            sudo_user=self._sudo_user,
            connection_url=admin_url,
        )
        if code != 0:
            msg = f"Failed to clone template: {stderr.strip()}"
            raise RuntimeError(msg)

        if self._connection_url:
            return replace_db_name(self._connection_url, test_db_name)
        return test_db_name

    def cleanup(self) -> int:
        """Remove template databases."""
        return cleanup_templates(
            self._env,
            prefix=self._template_prefix,
            max_templates=0,
            sudo_user=self._sudo_user,
            connection_url=self._admin_url(),
        )

    def status(self) -> TemplateStatus:
        """Return current template status."""
        current_hash = self._compute_hash()

        exists = check_db_exists(
            self.template_name,
            sudo_user=self._sudo_user,
            connection_url=self._admin_url(),
        )

        stored_hash = None
        built_at = None
        build_duration_ms = None

        if exists:
            meta = read_meta(
                self.template_name,
                connection_url=self._template_url(),
                sudo_user=self._sudo_user,
            )
            if meta:
                stored_hash = meta.schema_hash
                built_at = meta.built_at
                build_duration_ms = meta.build_duration_ms

        return TemplateStatus(
            template_name=self.template_name,
            template_exists=exists,
            current_hash=current_hash,
            stored_hash=stored_hash,
            needs_rebuild=stored_hash != current_hash,
            built_at=built_at,
            build_duration_ms=build_duration_ms,
        )


def _confiture_version() -> str:
    """Get installed confiture version."""
    try:
        from importlib.metadata import version

        return version("fraiseql-confiture")
    except Exception:
        return "unknown"

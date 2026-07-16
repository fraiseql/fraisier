"""Post-migration SQL hook (#204 PR A).

Runs a configurable list of SQL files (typically the project's idempotent
``db/7_grant/*.sql``) between ``confiture migrate up`` and the service
restart. Closes a class of regressions where a freshly-applied migration
introduces a new table that the app role has no grants on, or where a
``CREATE OR REPLACE VIEW`` applied by a non-owner role causes silent ACL
drift — both of which slip through unauthenticated ``/health`` but fail
the moment authenticated traffic hits.

Each step has an ``on_error`` knob:
- ``halt`` (default) — psql nonzero exit raises ``DeploymentError``
  immediately. No further entries run. Deploy aborts before the service
  is restarted, so there is nothing to roll back.
- ``warn`` — psql nonzero exit is logged at WARNING and iteration
  continues. The deploy still completes successfully.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fraisier.errors import DeploymentError

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    from fraisier.runners import CommandRunner

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PostMigrateStep:
    """One entry in the ``database.post_migrate`` list."""

    sql_dir: Path | None
    sql_file: Path | None
    on_error: Literal["halt", "warn"]


def _resolve(path: str, app_path: Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return app_path / p


def load_post_migrate_steps(
    database_config: dict,
    *,
    app_path: Path,
) -> list[PostMigrateStep]:
    """Parse ``database.post_migrate`` into a list of ``PostMigrateStep``.

    Returns an empty list if the section is missing or empty. Path
    fields are resolved relative to *app_path*. Validation of the
    overall shape (sql_dir XOR sql_file, on_error in {halt, warn}) is
    performed at config-load time by ``fraisier.config._validation``.
    """
    raw = database_config.get("post_migrate") or []
    steps: list[PostMigrateStep] = []
    for entry in raw:
        sql_dir_raw = entry.get("sql_dir")
        sql_file_raw = entry.get("sql_file")
        on_error: Literal["halt", "warn"] = entry.get("on_error", "halt")
        steps.append(
            PostMigrateStep(
                sql_dir=_resolve(sql_dir_raw, app_path) if sql_dir_raw else None,
                sql_file=_resolve(sql_file_raw, app_path) if sql_file_raw else None,
                on_error=on_error,
            )
        )
    return steps


def _expand_step(step: PostMigrateStep) -> Iterable[Path]:
    if step.sql_file is not None:
        yield step.sql_file
        return
    if step.sql_dir is not None:
        yield from sorted(step.sql_dir.glob("*.sql"))


def _run_one(
    sql_file: Path,
    *,
    database_url: str,
    runner: CommandRunner,
) -> None:
    cmd = [
        "psql",
        database_url,
        "-v",
        "ON_ERROR_STOP=1",
        "-f",
        str(sql_file),
    ]
    runner.run(cmd)


def run_post_migrate_steps(
    steps: list[PostMigrateStep],
    *,
    database_url: str,
    runner: CommandRunner,
) -> None:
    """Execute each step in order.

    A ``halt`` step that fails raises ``DeploymentError`` immediately
    and no further steps run. A ``warn`` step that fails is logged and
    iteration continues.
    """
    for step in steps:
        for sql_file in _expand_step(step):
            try:
                _run_one(sql_file, database_url=database_url, runner=runner)
            except subprocess.CalledProcessError as exc:
                if step.on_error == "halt":
                    raise DeploymentError(
                        f"post_migrate step failed (halt): {sql_file} "
                        f"(exit {exc.returncode}): {exc.stderr or ''}"
                    ) from exc
                logger.warning(
                    "post_migrate step %s failed (warn, continuing): %s",
                    sql_file,
                    exc.stderr or exc,
                )


def run_configured_post_migrate(
    database_config: dict,
    *,
    app_path: Path,
    runner: CommandRunner,
    database_url: str | None = None,
) -> None:
    """Load and run the configured ``database.post_migrate`` hooks.

    Single orchestration seam shared by the webhook deploy path
    (``deployers/api.py``) and the standalone ``fraisier db restore`` CLI
    (``cli/db.py``). The ``restore_migrate`` strategy restores with
    ``pg_restore --no-owner --no-acl``, so re-applying the configured grant
    scripts is what makes a restored database usable by non-owner roles
    (issue #273); before this seam only the deploy path re-applied them.

    A no-op when *database_url* cannot be resolved (no app DB to connect to)
    or the ``post_migrate`` list is empty. A ``halt`` step that fails raises
    ``DeploymentError``; a ``warn`` step logs and continues.
    """
    database_url = database_url or database_config.get("database_url")
    if not database_url:
        return
    steps = load_post_migrate_steps(database_config, app_path=app_path)
    if not steps:
        return
    run_post_migrate_steps(steps, database_url=database_url, runner=runner)

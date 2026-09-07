"""Post-migration schema-drift gate — does live match the DDL this checkout builds?

A migration can apply cleanly and still leave the database in a shape the code
does not expect.  ``CREATE OR REPLACE FUNCTION`` stores a **PL/pgSQL** body
without resolving what it references, so a DDL file whose function reads a
column the migration never added applies with exit 0; the deploy reports
success and the failure surfaces at that function's next call, minutes or weeks
later, far from the change that caused it.  ``confiture migrate validate
--check-live-drift`` sees it immediately.  This module is the wiring (#395).

**It belongs after ``migrate up``, never before.** ``--check-live-drift`` grades
expected (the DDL files) against actual (live), and rates "in the DDL, not in
live" — ``MISSING_TABLE`` / ``MISSING_COLUMN`` — CRITICAL.  A pending migration
that adds a table or column is, by definition, exactly that, so a pre-migration
gate fails closed on every deploy carrying one.  Measured on one entirely
legitimate pending migration: exit 1 before, exit 0 after.  Only the
post-migration position discriminates between a deploy in progress and a broken
schema.

Three parts of confiture's contract are sharp enough to name:

* ``confiture build`` takes ``--project-dir``/``--env``, ``migrate validate``
  takes **neither** — its ``--env`` resolves ``db/environments/{env}.yaml``
  against the *cwd*, and the deploy worker's cwd is not the checkout.  So the
  build is addressed by project directory and the validate by an **absolute**
  ``-c``, and :func:`_env_for_build` proves the two name the same file.
* **exit 1 is overloaded**: critical drift *and* "schema file not found".  The
  built file's existence is checked here, so a build that produced nothing can
  never be reported as a drifted database.
* the ``--format json`` payload **changes shape with the number of checks
  requested**: one check emits the bare report, two or more wrap them in a
  ``{"version", "status", "checks": {...}}`` envelope.  A reader that knows only
  the bare shape finds ``has_critical_drift`` absent in the envelope and calls a
  drifting database clean — so :func:`_reports` handles both and refuses
  anything else rather than defaulting to "no drift".
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

log = logging.getLogger(__name__)

#: Configured check name → the confiture flag it turns into.  ``--check-body-replay``
#: is deliberately absent: it replays every function body and belongs on a timer,
#: not in the path of a deploy.
CHECK_FLAGS: dict[str, str] = {
    "live-drift": "--check-live-drift",
    "signatures": "--check-signatures",
}

#: ``confiture build --env NAME`` resolves this path under ``--project-dir``.
_ENV_DIR = ("db", "environments")


@dataclass(frozen=True)
class DriftItem:
    """One difference between the built schema and the live database."""

    kind: str
    severity: str
    object_name: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity.upper()} {self.kind} {self.object_name}: {self.message}"


@dataclass(frozen=True)
class DriftResult:
    """The gate's verdict.

    ``passed`` is the only thing a caller should branch on.  It is false both
    for real drift and for a gate that could not reach a verdict — a check that
    did not run has not cleared anything, and reporting "no drift detected"
    because confiture was unreachable is the failure mode this gate exists to
    prevent.  ``error`` distinguishes the two for the operator.
    """

    passed: bool
    exit_code: int | None = None
    critical: tuple[DriftItem, ...] = ()
    warnings: tuple[DriftItem, ...] = ()
    error: str | None = None
    checks: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ran(self) -> bool:
        """True when confiture produced a verdict, whatever that verdict was."""
        return self.error is None

    def summary(self) -> str:
        """One line for a log, or several when there is drift to name."""
        if self.error is not None:
            return f"post_migrate_check could not run: {self.error}"
        if self.passed:
            note = f" ({len(self.warnings)} warning(s))" if self.warnings else ""
            return f"post_migrate_check: no critical schema drift{note}"
        named = "; ".join(str(item) for item in self.critical)
        return (
            f"post_migrate_check: {len(self.critical)} critical schema drift "
            f"item(s) after migration — {named}"
        )


def _env_for_build(project_dir: Path, confiture_config: Path) -> str:
    """The ``--env`` name whose file *is* ``confiture_config``.

    ``build`` cannot be pointed at a config path, only at an environment name it
    resolves under ``--project-dir``.  Deriving the name from the config's stem
    is only correct if the round trip lands back on the same file — otherwise
    the gate would compare the live database against *another environment's*
    DDL and report drift that says nothing about this deploy.

    Raises:
        ValueError: the config does not live at
            ``<project_dir>/db/environments/<name>.yaml``.
    """
    config = confiture_config.resolve()
    expected = project_dir.resolve().joinpath(*_ENV_DIR, config.name)
    if config != expected:
        msg = (
            f"post_migrate_check needs a confiture config that `confiture build "
            f"--env` can resolve: expected {expected}, got {config}. Point "
            f"database.confiture_config at db/environments/<env>.yaml, or "
            f"disable the gate."
        )
        raise ValueError(msg)
    return config.stem


def _connection_target(url: str) -> tuple[str, str, str]:
    """``(host, port, dbname)`` for *url*, for comparison only.

    PostgreSQL socket URLs carry the host in the query
    (``postgresql:///app?host=/run/postgresql``) rather than in the netloc, so
    both places are consulted before deciding two URLs disagree.
    """
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    host = parts.hostname or (query.get("host") or [""])[0]
    port = str(parts.port or (query.get("port") or [""])[0])
    return host, port, parts.path.lstrip("/")


def _check_urls_agree(confiture_config: Path, database_url: str | None) -> None:
    """Refuse when the gate would inspect a different database than was migrated.

    ``migrate validate`` has no ``--database-url``: it connects to whatever its
    ``-c`` config names.  fraisier's deploy, meanwhile, can override the URL from
    fraises.yaml, and that override never reaches the config file.  When the two
    name different databases the honest answer is to refuse — a gate that checks
    the wrong database and reports it clean is worse than no gate at all.

    Raises:
        ValueError: the two URLs name different databases.
    """
    if not database_url:
        return
    import yaml

    raw: Any = yaml.safe_load(confiture_config.read_text()) or {}
    configured = raw.get("database_url") if isinstance(raw, dict) else None
    if not configured:
        msg = (
            f"post_migrate_check cannot verify it would inspect the database "
            f"this deploy migrated: fraises.yaml sets database.database_url but "
            f"{confiture_config} declares no database_url, and `confiture "
            f"migrate validate` takes no --database-url. Declare it in the "
            f"confiture config, or disable the gate."
        )
        raise ValueError(msg)
    if _connection_target(configured) != _connection_target(database_url):
        msg = (
            f"post_migrate_check would inspect a different database than this "
            f"deploy migrated: {confiture_config} names "
            f"{_connection_target(configured)}, the deploy used "
            f"{_connection_target(database_url)}. Refusing rather than "
            f"reporting another database's schema as this one's."
        )
        raise ValueError(msg)


def _reports(stdout: str) -> list[dict[str, Any]]:
    """Every per-check report in *stdout*, whichever shape confiture emitted.

    One requested check produces the bare report; two or more produce an
    envelope keyed by check name.  Anything else raises: an unrecognised payload
    must not be read as "no drift".

    Raises:
        ValueError: the payload is not JSON, or is neither known shape.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        head = stdout.strip()[:200] or "<empty>"
        msg = f"confiture emitted no JSON report under --format json: {head}"
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"confiture's JSON report is not an object: {type(payload).__name__}"
        raise ValueError(msg)
    if isinstance(payload.get("checks"), dict):
        return [r for r in payload["checks"].values() if isinstance(r, dict)]
    if "has_critical_drift" in payload:
        return [payload]
    msg = (
        f"confiture's JSON report matches no known shape "
        f"(keys: {sorted(payload)}); refusing to read it as a clean schema"
    )
    raise ValueError(msg)


def _items(reports: Iterable[dict[str, Any]], severity: str) -> tuple[DriftItem, ...]:
    return tuple(
        DriftItem(
            kind=str(item.get("type", "unknown")),
            severity=str(item.get("severity", severity)),
            object_name=str(item.get("object", "?")),
            message=str(item.get("message", "")),
        )
        for report in reports
        for item in report.get("drift_items", [])
        if str(item.get("severity")) == severity
    )


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    log.debug("post_migrate_check: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def check_schema_drift(
    *,
    project_dir: Path,
    confiture_config: Path,
    checks: Sequence[str],
    database_url: str | None = None,
) -> DriftResult:
    """Compare the live database against the schema *project_dir* builds.

    Args:
        project_dir: The checkout, the same directory the migration ran in.
        confiture_config: The environment config the migration was driven from.
            Relative paths resolve against *project_dir*.
        checks: Names from :data:`CHECK_FLAGS`.
        database_url: The URL the migration used, when the deploy overrode it.
            Used only to refuse a check aimed at a different database.

    Returns:
        A :class:`DriftResult`.  It never raises for an operational failure —
        the caller's ``on_critical`` policy decides what a failed gate costs.
    """
    selected = tuple(checks)
    unknown = [name for name in selected if name not in CHECK_FLAGS]
    if unknown or not selected:
        return DriftResult(
            passed=False,
            checks=selected,
            error=(
                f"no runnable checks: {sorted(unknown)} not in {sorted(CHECK_FLAGS)}"
                if unknown
                else "no checks configured"
            ),
        )

    config = confiture_config
    if not config.is_absolute():
        config = (project_dir / config).resolve()

    try:
        env_name = _env_for_build(project_dir, config)
        _check_urls_agree(config, database_url)
    except (ValueError, OSError) as exc:
        return DriftResult(passed=False, checks=selected, error=str(exc))

    with tempfile.TemporaryDirectory(prefix="fraisier-drift-") as tmp:
        expected = Path(tmp) / "expected_schema.sql"
        build = _run(
            [
                "confiture",
                "build",
                "--project-dir",
                str(project_dir),
                "--env",
                env_name,
                "--schema-only",
                "--output",
                str(expected),
            ]
        )
        if build.returncode != 0:
            return DriftResult(
                passed=False,
                exit_code=build.returncode,
                checks=selected,
                error=(
                    f"could not build the expected schema for env {env_name!r} "
                    f"(confiture build exit {build.returncode}): "
                    f"{(build.stderr or build.stdout).strip()[:400]}"
                ),
            )
        if not expected.exists():
            # `migrate validate` reports a missing --schema file as exit 1, the
            # same code as critical drift. Catch it here, where the cause is
            # still legible, rather than reporting a clean database as drifted.
            return DriftResult(
                passed=False,
                checks=selected,
                error=(f"confiture build exited 0 but wrote no schema to {expected}"),
            )

        validate = _run(
            [
                "confiture",
                "migrate",
                "validate",
                *(CHECK_FLAGS[name] for name in selected),
                "-c",
                str(config),
                "--schema",
                str(expected),
                "--format",
                "json",
            ]
        )

    try:
        reports = _reports(validate.stdout)
    except ValueError as exc:
        stderr = validate.stderr.strip()[:200]
        detail = f"; {stderr}" if stderr else ""
        return DriftResult(
            passed=False,
            exit_code=validate.returncode,
            checks=selected,
            error=(
                f"{exc} (confiture migrate validate exit {validate.returncode}{detail})"
            ),
        )

    critical = _items(reports, "critical")
    warnings = _items(reports, "warning")
    drifted = any(report.get("has_critical_drift") for report in reports)
    return DriftResult(
        passed=not drifted,
        exit_code=validate.returncode,
        critical=critical,
        warnings=warnings,
        checks=selected,
    )

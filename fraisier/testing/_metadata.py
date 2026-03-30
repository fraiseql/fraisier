"""Metadata table management for test database templates.

Stores a single row in ``_fraisier_template_meta`` inside the template
database, recording the schema hash and build timing so that subsequent
test runs can skip rebuilds when the schema hasn't changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fraisier.dbops.operations import run_psql, run_sql

_TABLE = "_fraisier_template_meta"

_CREATE_DDL = f"""\
CREATE TABLE IF NOT EXISTS {_TABLE} (
    schema_hash TEXT NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    confiture_version TEXT,
    build_duration_ms INTEGER NOT NULL
)"""


@dataclass
class TemplateMeta:
    """Metadata recorded in the template database."""

    schema_hash: str
    built_at: datetime
    confiture_version: str
    build_duration_ms: int


def ensure_meta_table(
    db_name: str,
    *,
    connection_url: str | None = None,
    sudo_user: str = "postgres",
) -> None:
    """Create the metadata table if it doesn't exist."""
    code, _, stderr = run_psql(
        _CREATE_DDL,
        db_name=db_name,
        sudo_user=sudo_user,
        connection_url=connection_url,
    )
    if code != 0:
        msg = f"Failed to create metadata table: {stderr.strip()}"
        raise RuntimeError(msg)


def read_meta(
    db_name: str,
    *,
    connection_url: str | None = None,
    sudo_user: str = "postgres",
) -> TemplateMeta | None:
    """Read metadata from the template database.

    Returns ``None`` if the table is empty or the query fails.
    """
    sql = (
        f"SELECT schema_hash, built_at, confiture_version, build_duration_ms "
        f"FROM {_TABLE} LIMIT 1"
    )
    code, stdout, _ = run_sql(
        sql,
        db_name=db_name,
        sudo_user=sudo_user,
        connection_url=connection_url,
    )
    if code != 0:
        return None

    line = stdout.strip()
    if not line:
        return None

    parts = line.split("|")
    if len(parts) < 4:
        return None

    return TemplateMeta(
        schema_hash=parts[0],
        built_at=datetime.fromisoformat(parts[1]),
        confiture_version=parts[2],
        build_duration_ms=int(parts[3]),
    )


def write_meta(
    db_name: str,
    meta: TemplateMeta,
    *,
    connection_url: str | None = None,
    sudo_user: str = "postgres",
) -> None:
    """Write metadata to the template database (replaces any existing row)."""
    built_at_iso = meta.built_at.isoformat()
    sql = (
        f"TRUNCATE {_TABLE}; "
        f"INSERT INTO {_TABLE} "
        f"(schema_hash, built_at, confiture_version, build_duration_ms) "
        f"VALUES ('{meta.schema_hash}', '{built_at_iso}', "
        f"'{meta.confiture_version}', {meta.build_duration_ms})"
    )
    code, _, stderr = run_psql(
        sql,
        db_name=db_name,
        sudo_user=sudo_user,
        connection_url=connection_url,
    )
    if code != 0:
        msg = f"Failed to write metadata: {stderr.strip()}"
        raise RuntimeError(msg)

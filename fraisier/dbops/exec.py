"""SQL execution helpers for fraisier db exec."""

from __future__ import annotations

import re

_READONLY_PREFIXES = frozenset(["select", "explain", "show", "with", "table"])

_COMMENT_RE = re.compile(
    r"(/\*.*?\*/|--[^\n]*)[\s]*",
    re.DOTALL,
)


def _first_keyword(sql: str) -> str:
    """Return the first SQL keyword, ignoring leading comments/whitespace."""
    stripped = _COMMENT_RE.sub("", sql).strip().lower()
    if not stripped:
        return ""
    return stripped.split()[0]


def is_readonly_sql(sql: str) -> bool:
    """Return True if the SQL statement is safe to run without --write."""
    kw = _first_keyword(sql)
    return kw in _READONLY_PREFIXES


def build_psql_argv(
    db_name_or_url: str,
    sql: str,
    *,
    timeout_ms: int,
    output_format: str,
) -> list[str]:
    """Build the psql argv list for executing sql.

    Args:
        db_name_or_url: A plain database name or a full postgresql:// URL.
        sql:            The SQL string to execute.
        timeout_ms:     statement_timeout in milliseconds (always injected).
        output_format:  "table" | "csv" | "json"

    Returns:
        A list suitable for subprocess or ssh.short_cmd.
    """
    set_timeout = f"SET statement_timeout = {timeout_ms};"

    base = ["psql", "--no-psqlrc", "-d", db_name_or_url]

    if output_format == "csv":
        base += ["--csv", "-A"]
    elif output_format == "json":
        base += ["-t", "-A"]
        sql = f"SELECT row_to_json(t) FROM ({sql.rstrip(';')}) t"

    base += ["-c", f"{set_timeout} {sql}"]
    return base

"""Sample ``pg_database_size(current_database())`` for the duration estimator.

Best-effort: any psql failure (connectivity, auth, binary missing, parse
error) returns ``None`` so the caller's write path is never blocked. The
duration estimator falls back to its per-strategy floor when the column
is NULL — deploys continue to work, but the history-aware estimate
loses a signal until the config that broke the lookup is fixed.

Used by ``fraisier.deployers.mixins._complete_db_record`` after each
successful deploy with a configured ``database.database_url``.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:  # pragma: no cover
    from fraisier.runners import CommandRunner

logger = logging.getLogger(__name__)

_BYTES_PER_MB = 1024 * 1024


def _redact_url(database_url: str) -> str:
    """Return ``scheme://user@host/dbname`` — drops the password component.

    Falls back to ``"<unparseable>"`` if ``database_url`` cannot be split.
    """
    try:
        parsed = urlparse(database_url)
        userinfo = f"{parsed.username}@" if parsed.username else ""
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://{userinfo}{host}{parsed.path}"
    except ValueError:
        return "<unparseable>"


def query_database_size_mb(
    database_url: str,
    *,
    runner: CommandRunner,
) -> int | None:
    """Return current app DB size in MB via ``pg_database_size``, or ``None``.

    Shells out to ``psql`` against *database_url*; any failure
    (CalledProcessError, OSError, parse error) returns ``None`` with a
    debug-level log entry. The log entry redacts the password component
    of *database_url* so the misconfig is diagnosable without leaking
    credentials.
    """
    cmd = [
        "psql",
        database_url,
        "-tAc",
        "SELECT pg_database_size(current_database())",
    ]
    try:
        result = runner.run(cmd)
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.debug(
            "query_database_size_mb: psql failed for %s: %s",
            _redact_url(database_url),
            exc,
        )
        return None

    try:
        size_bytes = int(result.stdout.strip())
    except (AttributeError, ValueError) as exc:
        logger.debug(
            "query_database_size_mb: unparseable psql output for %s: %r (%s)",
            _redact_url(database_url),
            getattr(result, "stdout", None),
            exc,
        )
        return None

    return size_bytes // _BYTES_PER_MB

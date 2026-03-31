"""PostgreSQL URL utilities.

Handles database name substitution in connection URLs, correctly
preserving the triple-slash syntax required for Unix socket connections
(e.g. ``postgresql:///dbname?host=/var/run/postgresql``).
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def replace_db_name(url: str, db_name: str) -> str:
    """Return *url* with the database name replaced by *db_name*.

    ``urllib.parse.urlunparse`` collapses ``scheme:///path`` (empty netloc)
    into ``scheme:/path``, which is invalid for PostgreSQL socket URLs.
    This function preserves the original ``://`` or ``:///`` prefix.
    """
    parsed = urlparse(url)
    replaced = urlunparse(parsed._replace(path=f"/{db_name}"))

    # urlunparse with an empty netloc produces "scheme:/path" instead of
    # "scheme:///path".  Detect and fix.
    if parsed.netloc == "" and f"{parsed.scheme}:///" in url:
        replaced = replaced.replace(f"{parsed.scheme}:/", f"{parsed.scheme}:///", 1)

    return replaced

"""PostgreSQL URL utilities.

Handles database name substitution in connection URLs, correctly
preserving the triple-slash syntax required for Unix socket connections
(e.g. ``postgresql:///dbname?host=/var/run/postgresql``).

Contract for ``connection_url`` parameters in ``dbops/`` and
``strategies/``: every ``connection_url`` (or ``database_url`` /
``admin_url``) parameter is a concrete ``str``, never a
:class:`fraisier.config.LazyEnv`. Resolution happens at strategy
entry via :func:`resolve_db_url`; downstream signatures stay typed
``str`` so a stray ``LazyEnv`` is a type error, not a silent
``str()``-via-coercion at the wrong layer.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse, urlunparse

from fraisier.config._lazy_env import LazyEnv, to_str


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


def resolve_db_url(
    value: str | LazyEnv | None,
    *,
    role: Literal["database_url", "admin_url"] = "database_url",
) -> str | None:
    """Resolve a strategy-entry DB URL to a concrete ``str``.

    Returns ``None`` unchanged so callers' ``if not database_url:``
    branches keep working. A :class:`LazyEnv` is resolved at this
    boundary so every dbops call downstream receives a concrete URL —
    no ``str | LazyEnv`` union creeps through the ~70 propagation
    sites in dbops/strategies.

    *role* shows up in the searchable trace when a future audit asks
    "where do we resolve admin_url vs database_url?" but doesn't change
    the resolution itself.
    """
    del role  # informational; kept for grep traceability
    if value is None:
        return None
    return to_str(value)

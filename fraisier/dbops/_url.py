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
from urllib.parse import urlparse, urlsplit, urlunparse

from fraisier.config._lazy_env import LazyEnv, to_str
from fraisier.dbops._pgconn import PgConn, decode, split_netloc, split_param


def replace_db_name(url: str, db_name: str) -> str:
    """Return *url* with the database name replaced by *db_name*.

    A ``dbname=`` query parameter is dropped: libpq reads the query after the
    path, so it would name the database instead.

    ``urllib.parse.urlunparse`` collapses ``scheme:///path`` (empty netloc)
    into ``scheme:/path``, which is invalid for PostgreSQL socket URLs.
    This function preserves the original ``://`` or ``:///`` prefix.
    """
    parsed = urlparse(url)
    query = "&".join(
        param
        for param in parsed.query.split("&")
        if param and decode(param.partition("=")[0]) != "dbname"
    )
    replaced = urlunparse(parsed._replace(path=f"/{db_name}", query=query))

    # urlunparse with an empty netloc produces "scheme:/path" instead of
    # "scheme:///path".  Detect and fix.
    if parsed.netloc == "" and f"{parsed.scheme}:///" in url:
        replaced = replaced.replace(f"{parsed.scheme}:/", f"{parsed.scheme}:///", 1)

    return replaced


def move_password_to_env(url: str) -> tuple[str, dict[str, str]]:
    """Return *url* without its password, and the env that carries it instead.

    A child's argv is readable by every local user (``ps``,
    ``/proc/<pid>/cmdline``), so a URL handed to one must not carry a password.
    libpq falls back to ``PGPASSWORD`` when the connection string has none.

    The password may be in the userinfo or in a ``password=`` query parameter.
    libpq reads the query second, so the query's value wins. Both are decoded
    by :func:`fraisier.dbops._pgconn.decode`, libpq's rules: ``+`` stays ``+``,
    and a malformed escape, ``%00``, a second unencoded ``@`` or a parameter
    without ``=`` raise ``ValueError`` instead of reaching libpq mangled.
    The URL is rebuilt by hand, because ``urlunsplit`` turns
    ``postgresql:///db`` into ``postgresql:/db``. A key=value conninfo string
    is returned unchanged.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("postgresql", "postgres"):
        return url, {}

    password: str | None = None
    netloc = parts.netloc
    userinfo, hostport = split_netloc(netloc)
    if ":" in userinfo:
        user, _, raw = userinfo.partition(":")
        password = decode(raw)
        netloc = f"{user}@{hostport}"

    kept: list[str] = []
    for param in parts.query.split("&") if parts.query else []:
        key, value = split_param(param)
        if decode(key) == "password":
            password = decode(value)
        else:
            kept.append(param)

    if password is None:
        return url, {}
    rebuilt = f"{parts.scheme}://{netloc}{parts.path}"
    if kept:
        rebuilt += "?" + "&".join(kept)
    if parts.fragment:
        rebuilt += "#" + parts.fragment
    return rebuilt, {"PGPASSWORD": password} if password else {}


def url_child(url: str) -> tuple[str, dict[str, str]]:
    """``(argv_url, env)`` for a child that takes its connection URL on argv.

    confiture's interface, and some psql calls, take a URL, so it stays on
    argv: *url* itself, password removed, byte for byte otherwise. It is first
    parsed by :class:`~fraisier.dbops._pgconn.PgConn`, so a URL libpq would
    read differently is refused here, and the child's environment is
    :meth:`PgConn.child_env` — the password, and no ambient ``PG*`` beyond the
    operator defaults.
    """
    conn = PgConn.parse(url)
    argv_url, _ = move_password_to_env(url)
    return argv_url, conn.child_env()


def url_runner_child(url: str) -> tuple[str, dict[str, str]]:
    """:func:`url_child` for a child launched through a ``CommandRunner``.

    The env is the URL's own ``PG*`` (:meth:`PgConn.pg_env`) and nothing of
    ``os.environ``, which a remote runner would ship to its host. Launch it
    with ``scrub_pg=True``, so the runner drops whatever ambient ``PG*`` the
    child's own environment holds.
    """
    conn = PgConn.parse(url)
    argv_url, _ = move_password_to_env(url)
    return argv_url, conn.pg_env()


#: How a deploy's log and record name each URL's role.
_ROLE_LABELS = {"database_url": "app", "admin_url": "admin"}


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

    The resolved URL is parsed by :class:`~fraisier.dbops._pgconn.PgConn`, so
    one libpq would read differently is refused here, before the strategy has
    done anything (a dump restore stops the service before its first child).
    *role* names the field in that refusal.

    Raises:
        ValueError: naming *role* and what is wrong, never the password.
    """
    if value is None:
        return None
    url = to_str(value)
    try:
        PgConn.parse(url)
    except ValueError as exc:
        msg = f"{role}: {exc}"
        raise ValueError(msg) from None
    from fraisier.dbops import identity

    identity.note_connection(url, _ROLE_LABELS[role])
    return url

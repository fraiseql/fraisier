"""pg_tviews read surface: is it usable here, what is empty, what to rebuild (#422).

A TVIEW is a table pg_tviews keeps in step with a backing view.  An UNLOGGED one
is emptied by a crash-recovery start, a failover or a physical restore, and
nothing then tells the application: every count passes and the read model is
simply gone.  This module is how fraisier sees that, and the one place that
knows how pg_tviews is spoken to.

Everything goes through pg_tviews' documented read contract 1 — the
``tviews.registry`` view and ``contract_version()`` — never ``pg_tview_meta``,
and never a name inferred from another (``v_<entity>`` from ``tv_<entity>``).
The extension's schema is read from the catalog rather than assumed to be
``tviews``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import psycopg
from psycopg import sql

if TYPE_CHECKING:
    from psycopg import Connection

log = logging.getLogger(__name__)

#: The read contract fraisier speaks.  confiture 1.29 requires the same one.
CONTRACT = 1

_CONNECT_TIMEOUT_SECONDS = 10


class _Executes(Protocol):
    """The one thing this module asks of a connection."""

    def execute(self, query: Any, params: Any = None) -> Any: ...


class TviewError(RuntimeError):
    """A pg_tviews call failed, with a message an operator can act on."""


@dataclass(frozen=True)
class TviewsSupport:
    """What a database offers: ``absent``, ``outdated`` (no contract 1) or ``ok``."""

    state: Literal["absent", "outdated", "ok"]
    schema: str | None = None


@dataclass(frozen=True)
class TviewRebuilt:
    """One entity ``pg_tviews_rebuild_all`` filled, and the row count it reported.

    The count is the TVIEW's size afterwards up to pg_tviews 0.1.0-beta.28, and
    from beta.29 the rows the call inserted (#472).  They differ only for a
    TVIEW that was refilled while it still held rows, which beta.27 does after a
    dump restore and reports as its full size.  No caller branches on which.
    """

    entity: str
    rows: int


@dataclass(frozen=True)
class EmptyTview:
    """A TVIEW with no rows whose backing view has some."""

    schema: str
    name: str
    view_schema: str
    view_name: str

    @property
    def tview(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def view(self) -> str:
        return f"{self.view_schema}.{self.view_name}"


def read_support(conn: _Executes) -> TviewsSupport:
    """Classify *conn*'s database: no pg_tviews, pg_tviews without contract 1, or ready.

    ``pg_extension.extversion`` is no help — it reads ``0.1.0`` on every beta —
    so this asks the function confiture asks.  Run on an autocommit connection:
    a missing ``contract_version()`` is an error that would otherwise poison the
    transaction.
    """
    row = conn.execute(
        "SELECT n.nspname FROM pg_extension e "
        "JOIN pg_namespace n ON n.oid = e.extnamespace "
        "WHERE e.extname = 'pg_tviews'"
    ).fetchone()
    if row is None:
        return TviewsSupport("absent")
    schema = str(row[0])
    try:
        contract = conn.execute(
            sql.SQL("SELECT {}.contract_version()").format(sql.Identifier(schema))
        ).fetchone()
    except psycopg.Error:
        return TviewsSupport("outdated", schema)
    if contract is None or contract[0] != CONTRACT:
        return TviewsSupport("outdated", schema)
    return TviewsSupport("ok", schema)


def _connect(url: str) -> Connection:
    return psycopg.connect(
        url, autocommit=True, connect_timeout=_CONNECT_TIMEOUT_SECONDS
    )


def tviews_installed(url: str) -> bool:
    """True only when pg_tviews is present **and** speaks contract 1."""
    with _connect(url) as conn:
        return read_support(conn).state == "ok"


_HINT_OUTDATED = (
    "pg_tviews here predates read contract 1; upgrade it to 0.1.0-beta.20 or "
    "later and run its scripts/migrate-from-0.1.0.sql"
)


def _usable(conn: _Executes) -> TviewsSupport | None:
    """The support record when pg_tviews is ready, ``None`` when it is absent.

    An outdated pg_tviews raises: answering "nothing is empty" from a database
    whose TVIEWs fraisier cannot read would be the clean bill this module exists
    to refuse.
    """
    support = read_support(conn)
    if support.state == "outdated":
        raise TviewError(_HINT_OUTDATED)
    return support if support.state == "ok" else None


def find_empty_tviews(
    url: str, *, on_unreadable: Literal["warn", "fail"] = "warn"
) -> list[EmptyTview]:
    """Every TVIEW with no rows whose backing view has some.

    confiture's drift is schema-only, so this is the data probe it cannot be.
    Both the TVIEW and its view come from ``registry``, which carries the view
    as a ``regclass`` — nothing is inferred from a name.  A TVIEW that is empty
    because its view is empty is correct and is not reported.

    A TVIEW the connecting role cannot read, or whose backing view it cannot
    read, is neither empty nor fine: the probe cannot tell.  It is logged by name
    with the grant that fixes it and skipped, never failed, so the TVIEWs the role
    can read are still checked.  pg_tviews 0.1.0-beta.25 moves the backing view
    into its own schema and gives it its table's ``SELECT`` grants, so this is
    what a role that read the old ``v_<entity>`` only through a grant on the view
    meets.  Any other error still propagates.

    *on_unreadable* is what such a TVIEW costs.  ``warn`` (the default) is the
    above.  ``fail`` is for a gate whose owner chose ``on_empty: fail``: a TVIEW
    nobody can look at has cleared nothing, so the probe raises
    :class:`TviewError` naming each one and the grant that fixes it.

    Returns ``[]`` when the database has no pg_tviews.
    """
    with _connect(url) as conn:
        support = _usable(conn)
        if support is None:
            return []
        registry = conn.execute(
            sql.SQL(
                "SELECT r.schema, r.name, vn.nspname, vc.relname "
                "FROM {}.registry r "
                "JOIN pg_class vc ON vc.oid = r.view "
                "JOIN pg_namespace vn ON vn.oid = vc.relnamespace "
                "ORDER BY r.schema, r.name"
            ).format(sql.Identifier(str(support.schema)))
        ).fetchall()
        empty: list[EmptyTview] = []
        unreadable: list[str] = []
        for schema, name, view_schema, view_name in registry:
            try:
                row = conn.execute(
                    sql.SQL(
                        "SELECT EXISTS (SELECT 1 FROM {tv}), EXISTS (SELECT 1 FROM {v})"
                    ).format(
                        tv=sql.Identifier(schema, name),
                        v=sql.Identifier(view_schema, view_name),
                    )
                ).fetchone()
            except psycopg.errors.InsufficientPrivilege:
                grant = _grant_hint(conn, schema, name)
                log.warning(
                    "pg_tviews: cannot verify %s.%s (backing view %s.%s): "
                    "permission denied; skipped. Fix: %s; the backing view "
                    "follows its table's grants",
                    schema,
                    name,
                    view_schema,
                    view_name,
                    grant,
                )
                unreadable.append(
                    f"{schema}.{name} (view {view_schema}.{view_name}): {grant}"
                )
                continue
            has_rows, view_has_rows = row if row is not None else (True, False)
            if not has_rows and view_has_rows:
                empty.append(EmptyTview(schema, name, view_schema, view_name))
        if unreadable and on_unreadable == "fail":
            raise TviewError(
                f"{len(unreadable)} pg_tviews TVIEW(s) cannot be read by this role, "
                f"so they were not checked for emptiness: {'; '.join(unreadable)}"
            )
        return empty


def list_tviews(url: str) -> list[tuple[str, str]]:
    """Every TVIEW in the registry, as ``(schema, name)``, sorted.

    What a lean dump leaves the data of out (#471).  It comes from the
    registry and never from a ``tv_*`` name pattern: a plain table may carry
    that name, and fraiseql v1 schemas are full of them.  ``[]`` when the
    database has no pg_tviews.  Raises :class:`TviewError` for a pg_tviews this
    module cannot read, because "no TVIEWs" would then dump everything quietly.
    """
    with _connect(url) as conn:
        support = _usable(conn)
        if support is None:
            return []
        rows = conn.execute(
            sql.SQL("SELECT r.schema, r.name FROM {}.registry r ORDER BY 1, 2").format(
                sql.Identifier(str(support.schema))
            )
        ).fetchall()
        return [(str(schema), str(name)) for schema, name in rows]


def _grant_hint(conn: _Executes, schema: str, name: str) -> str:
    """The ``GRANT`` that lets the connecting role read a TVIEW, ready to paste."""
    row = conn.execute("SELECT current_user").fetchone()
    role = row[0] if row else "<role>"
    return (
        sql.SQL("GRANT SELECT ON {t} TO {r}")
        .format(t=sql.Identifier(schema, name), r=sql.Identifier(role))
        .as_string()
    )


def _rebuild_grant(conn: _Executes, schema: str) -> tuple[str, str]:
    """The rebuild function's signature, and the ``GRANT`` letting this role run it."""
    row = conn.execute("SELECT current_user").fetchone()
    role = row[0] if row else "<role>"
    function = (
        sql.SQL("{}.pg_tviews_rebuild_all(boolean)")
        .format(sql.Identifier(schema))
        .as_string()
    )
    grant = (
        sql.SQL("GRANT EXECUTE ON FUNCTION {}.pg_tviews_rebuild_all(boolean) TO {}")
        .format(sql.Identifier(schema), sql.Identifier(role))
        .as_string()
    )
    return function, grant


def rebuild_privilege_problem(url: str) -> str | None:
    """Why the connecting role cannot run ``pg_tviews_rebuild_all``, or ``None``.

    pg_tviews 0.1.0-beta.27 stops granting its bulk maintenance functions to
    ``PUBLIC`` (#460), so a restore role that is neither a superuser nor the
    extension's owner meets ``42501`` at the rebuild — after the restore, when
    the database it replaced is already gone.  This asks the same question with
    ``has_function_privilege`` while there is still time to refuse, and answers
    with the ``GRANT`` that fixes it.

    ``None`` when the database has no pg_tviews.  Raises :class:`TviewError` for a
    pg_tviews without read contract 1, like every other call here.
    """
    with _connect(url) as conn:
        support = _usable(conn)
        if support is None:
            return None
        return _missing_rebuild_grant(conn, str(support.schema))


def _missing_rebuild_grant(conn: _Executes, schema: str) -> str | None:
    function, grant = _rebuild_grant(conn, schema)
    row = conn.execute(
        "SELECT has_function_privilege(%s, 'EXECUTE'), current_user", (function,)
    ).fetchone()
    if row is None or row[0]:
        return None
    return (
        f"role {row[1]!r} cannot execute {function}, which the post-restore "
        f"TVIEW rebuild runs (pg_tviews 0.1.0-beta.27 no longer grants it to "
        f"PUBLIC). Fix: {grant}"
    )


def _rebuild(url: str, *, only_empty: bool) -> list[TviewRebuilt]:
    with _connect(url) as conn:
        support = _usable(conn)
        if support is None:
            return []
        recovering = conn.execute("SELECT pg_is_in_recovery()").fetchone()
        if recovering is not None and recovering[0]:
            raise TviewError(
                "the database is in recovery (a standby, or still starting); "
                "TVIEWs can only be rebuilt on the primary — run this again "
                "after promotion"
            )
        try:
            cursor = conn.execute(
                sql.SQL(
                    "SELECT * FROM {}.pg_tviews_rebuild_all(only_empty => %s)"
                ).format(sql.Identifier(str(support.schema))),
                (only_empty,),
            )
            rows = cursor.fetchall()
        except psycopg.errors.InsufficientPrivilege as exc:
            # Only name the function grant when it is the one missing: a refusal
            # on a table a TVIEW reads is the same SQLSTATE.
            missing = _missing_rebuild_grant(conn, str(support.schema))
            raise TviewError(
                f"pg_tviews_rebuild_all failed: {exc}. "
                + (
                    missing
                    or "pg_tviews rebuilds each TVIEW as its owner, so that owner "
                    "lacks a privilege on what the TVIEW reads; grant it to the "
                    "owner"
                )
            ) from exc
        except psycopg.Error as exc:
            raise TviewError(f"pg_tviews_rebuild_all failed: {exc}") from exc
        names = [column.name for column in cursor.description or ()]
        entity, count = _rebuild_columns(names)
        return [TviewRebuilt(str(row[entity]), int(row[count])) for row in rows]


#: The count's name, newest first: beta.29 renamed ``rows`` (#472).
_REBUILD_COUNT_COLUMNS = ("rows_inserted", "rows")


def _rebuild_columns(names: list[str]) -> tuple[int, int]:
    """Where the entity and the count sit in ``pg_tviews_rebuild_all``'s result."""
    count = next((c for c in _REBUILD_COUNT_COLUMNS if c in names), None)
    if "entity" not in names or count is None:
        raise TviewError(
            f"pg_tviews_rebuild_all returned columns {', '.join(names)}; fraisier "
            f"reads entity and one of {', '.join(_REBUILD_COUNT_COLUMNS)}"
        )
    return names.index("entity"), names.index(count)


def rebuild_empty_tviews(url: str) -> list[TviewRebuilt]:
    """Fill every UNLOGGED TVIEW PostgreSQL reset (a crash, failover or restore).

    ``pg_tviews_rebuild_all(only_empty => true)``: dependencies first, no
    ``TRUNCATE`` so readers are not blocked, and a healthy TVIEW is left alone.
    ``[]`` means nothing needed rebuilding — or the database has no pg_tviews;
    callers that must tell those apart ask :func:`tviews_installed`.

    Raises :class:`TviewError` on a standby, which pg_tviews refuses.
    """
    return _rebuild(url, only_empty=True)


def rebuild_all_tviews(url: str) -> list[TviewRebuilt]:
    """Rebuild **every** TVIEW from its backing view, empty or not.

    The repair for a TVIEW the probe reports that ``only_empty`` will not touch
    (a LOGGED one, or one emptied by hand), and for one suspected stale.
    """
    return _rebuild(url, only_empty=False)


def profile_tviews(url: str) -> list[dict[str, Any]]:
    """``pg_tviews_profile()``: size, bloat, HOT ratio and warnings per TVIEW.

    Read-only, and callable on a standby — which is the point: it is what an
    operator looks at *before* deciding to rebuild.  Rows are keyed by the
    function's own column names, so a column pg_tviews adds reaches the caller
    without a change here.  ``[]`` when the database has no pg_tviews.
    """
    with _connect(url) as conn:
        support = _usable(conn)
        if support is None:
            return []
        cursor = conn.execute(
            sql.SQL("SELECT * FROM {}.pg_tviews_profile()").format(
                sql.Identifier(str(support.schema))
            )
        )
        names = [column.name for column in cursor.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import psycopg
from psycopg import sql

if TYPE_CHECKING:
    from psycopg import Connection

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
    """One entity ``pg_tviews_rebuild_all`` filled, and how many rows it holds now."""

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


def find_empty_tviews(url: str) -> list[EmptyTview]:
    """Every TVIEW with no rows whose backing view has some.

    confiture's drift is schema-only, so this is the data probe it cannot be.
    Both the TVIEW and its view come from ``registry``, which carries the view
    as a ``regclass`` — nothing is inferred from a name.  A TVIEW that is empty
    because its view is empty is correct and is not reported.

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
        for schema, name, view_schema, view_name in registry:
            row = conn.execute(
                sql.SQL(
                    "SELECT EXISTS (SELECT 1 FROM {tv}), EXISTS (SELECT 1 FROM {v})"
                ).format(
                    tv=sql.Identifier(schema, name),
                    v=sql.Identifier(view_schema, view_name),
                )
            ).fetchone()
            has_rows, view_has_rows = row if row is not None else (True, False)
            if not has_rows and view_has_rows:
                empty.append(EmptyTview(schema, name, view_schema, view_name))
        return empty


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
            rows = conn.execute(
                sql.SQL(
                    "SELECT entity, rows FROM {}.pg_tviews_rebuild_all"
                    "(only_empty => %s)"
                ).format(sql.Identifier(str(support.schema))),
                (only_empty,),
            ).fetchall()
        except psycopg.Error as exc:
            raise TviewError(f"pg_tviews_rebuild_all failed: {exc}") from exc
        return [TviewRebuilt(str(entity), int(count)) for entity, count in rows]


def rebuild_empty_tviews(url: str) -> list[TviewRebuilt]:
    """Fill every UNLOGGED TVIEW that is empty while its backing view is not.

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
    (a LOGGED one), and for a TVIEW suspected stale rather than empty.
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

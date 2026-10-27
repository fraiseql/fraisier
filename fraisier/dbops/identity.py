"""Who each deploy connection actually connects as.

A URL says which user and database it means; libpq decides which it gets. A
user-less socket URL connects as the OS account, and before ``PgConn`` an
ambient ``PGUSER`` or service file could quietly change either. So a deploy
asks the server, once per distinct URL, at the URL's first use: through
``_pg_cmd``, launched exactly as every other child is, so the answer is the
identity the children get.

The answers are logged at INFO and kept per role for the deployment record,
which ``fraisier status`` reads without connecting. Outside a deploy nothing
is probed: :func:`note_connection` is a no-op until :func:`recording` is
entered.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from fraisier.dbops._pgconn import PgConn
from fraisier.dbops.operations import _pg_cmd

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger("fraisier")

_SQL = "SELECT current_user, current_database(), inet_server_addr()"

#: Seconds a probe may take: an unreachable server must not hold a deploy for
#: the kernel's TCP timeout before its first real child meets the same wait.
PROBE_TIMEOUT = 15.0


@dataclass(frozen=True)
class ConnectedAs:
    """The server's own answer for one URL, or why there is none."""

    user: str | None = None
    database: str | None = None
    host: str | None = None
    error: str | None = None

    def as_record(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def probe_identity(url: str) -> ConnectedAs:
    """Ask the server *url* reaches who and where this connection is.

    Never raises: a probe is a report, and a deploy must not fail on one.
    ``inet_server_addr()`` is NULL over a unix socket, where the URL's socket
    directory, or libpq's default one, is named instead.
    """
    try:
        code, out, err = _pg_cmd(
            ["psql", "-XtA", "-c", _SQL], connection_url=url, timeout=PROBE_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        return ConnectedAs(error=f"no answer within {PROBE_TIMEOUT:g}s")
    except (OSError, ValueError) as exc:
        return ConnectedAs(error=str(exc))
    if code != 0:
        return ConnectedAs(error=err.strip() or f"psql exited {code}")
    user, database, address = (*out.strip().split("|"), "", "", "")[:3]
    host = address or PgConn.parse(url).host or "local socket"
    return ConnectedAs(user=user, database=database, host=host)


@dataclass
class _Recorder:
    by_role: dict[str, ConnectedAs] = field(default_factory=dict)
    by_url: dict[str, ConnectedAs] = field(default_factory=dict)


_ACTIVE: ContextVar[_Recorder | None] = ContextVar("fraisier_identities", default=None)


@contextlib.contextmanager
def recording() -> Iterator[None]:
    """Probe and keep identities for the connections noted inside."""
    token = _ACTIVE.set(_Recorder())
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def note_connection(url: str, role: str) -> None:
    """A deploy is about to use *url* as *role*: probe it, the first time.

    A URL is probed once however many roles use it. The first URL noted for a
    role is the one recorded for it.
    """
    recorder = _ACTIVE.get()
    if recorder is None:
        return
    found = recorder.by_url.get(url)
    if found is None:
        found = recorder.by_url[url] = probe_identity(url)
        if found.error is None:
            logger.info(
                "connected as %s to %s on %s (role: %s)",
                found.user,
                found.database,
                found.host,
                role,
            )
        else:
            logger.warning(
                "could not tell who role %s connects as: %s", role, found.error
            )
    recorder.by_role.setdefault(role, found)


def recorded() -> dict[str, dict[str, str]] | None:
    """The identities kept so far, per role, or None outside a deploy."""
    recorder = _ACTIVE.get()
    if recorder is None:
        return None
    return {role: found.as_record() for role, found in recorder.by_role.items()}

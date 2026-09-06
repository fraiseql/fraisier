"""Integration test fixtures.

``flock_holder`` spawns a subprocess that takes a real ``fcntl.flock`` on a
named lock file and holds it until a release event fires. Used by the
self-upgrade drain regression test to model an in-flight deploy without
patching the locking primitives.

``pg_target`` yields a PostgreSQL server these tests may create databases on,
and skips when there is none. Shared because more than one end-to-end restore
test needs a real ``pg_dump``/``pg_restore`` round trip and "is a database
reachable here" is one fact, not one per test module.

It discovers **two transports**, and that is not a convenience. A developer runs
these against a local server over a unix socket; every CI workflow provides one
as a service container reachable over TCP only, with no socket shared into the
runner. A harness that looked for a socket alone found nothing in CI, the
fixture skipped, and eighteen tests carrying #358's central guarantee reported
green without ever executing. Discovery therefore takes ``FRAISIER_TEST_PG_URL``
first, and :class:`PgTarget` builds per-database DSNs over whichever transport
was found — so no test module has to know which one it got.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import quote, unquote, urlparse

import pytest

from fraisier.locking import file_deployment_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

#: Probed and created against, because that is what the tests do: every one of
#: them connects here to ``CREATE``/``DROP`` its own database.
_MAINTENANCE_DB = "postgres"

_CANDIDATE_SOCKET_DSNS = (
    f"postgresql:///{_MAINTENANCE_DB}",
    f"postgresql:///{_MAINTENANCE_DB}?host=/run/postgresql",
    f"postgresql:///{_MAINTENANCE_DB}?host=/var/run/postgresql",
    f"postgresql:///{_MAINTENANCE_DB}?host=/tmp",
)


@dataclass(frozen=True)
class PgTarget:
    """A server the integration tests may create databases on.

    ``host`` is either a directory — libpq reads a leading ``/`` as a unix
    socket directory — or a hostname reached over TCP. ``dsn`` is the only thing
    a test needs: it hands back a URL for one database, in the form the
    discovered transport requires.
    """

    host: str
    port: int | None = None
    user: str | None = None
    password: str | None = None

    @property
    def over_socket(self) -> bool:
        return self.host.startswith("/")

    def dsn(self, db_name: str) -> str:
        """A connection URL for *db_name* on this server."""
        if self.over_socket:
            return f"postgresql:///{db_name}?host={self.host}"
        netloc = f"{self.host}:{self.port}" if self.port else self.host
        if self.user:
            credentials = quote(self.user, safe="")
            if self.password:
                credentials = f"{credentials}:{quote(self.password, safe='')}"
            netloc = f"{credentials}@{netloc}"
        return f"postgresql://{netloc}/{db_name}"


def _probe(dsn: str) -> str | None:
    """Is *dsn* usable for these tests, and where is that server's socket?

    Returns the server's first unix socket directory, or None when the DSN did
    not connect or the role it connects as cannot create databases — both of
    which make an end-to-end restore test inapplicable rather than failed.

    The socket directory is returned because a socket candidate has to be
    re-addressed by the path the *server* reports rather than the one that
    happened to connect: ``postgresql:///postgres`` carries no host at all, and
    ``pg_dump`` needs somewhere to point.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            socket_dirs, can_create = conn.execute(
                "SELECT current_setting('unix_socket_directories'), "
                "(SELECT rolcreatedb OR rolsuper FROM pg_roles "
                " WHERE rolname = current_user)"
            ).fetchone()
    except Exception:
        return None
    if not can_create:
        return None
    return socket_dirs.split(",")[0].strip()


def _env_target() -> PgTarget | None:
    """The server named by ``FRAISIER_TEST_PG_URL``, if it names one."""
    parsed = urlparse(os.getenv("FRAISIER_TEST_PG_URL", ""))
    if not parsed.hostname:
        return None
    return PgTarget(
        host=parsed.hostname,
        port=parsed.port,
        user=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
    )


def _client_tools_present() -> bool:
    """Are the CLI tools these tests shell out to on PATH?

    A container cannot substitute for them: the dump/restore round trips run
    the *host's* ``pg_dump`` and ``pg_restore``.
    """
    return all(shutil.which(tool) for tool in ("psql", "pg_dump", "pg_restore"))


def client_major_version() -> int | None:
    """The major version of the host's ``pg_dump``, or None if unreadable."""
    try:
        out = subprocess.run(
            ["pg_dump", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)", out)
    return int(match.group(1)) if match else None


def _discover_target() -> PgTarget | None:
    """Return a *running* server to use, or None if there is none.

    ``FRAISIER_TEST_PG_URL`` is tried first — it is the server CI provides — and
    a local socket second. An env URL that does not answer falls through rather
    than deciding the question, so a stale variable cannot hide a working local
    server. A container is not tried here; see :func:`_container_target`, which
    only runs once nothing already-running answered.
    """
    pytest.importorskip("psycopg")
    if not _client_tools_present():
        return None

    from_env = _env_target()
    if from_env is not None and _probe(from_env.dsn(_MAINTENANCE_DB)) is not None:
        return from_env

    for dsn in _CANDIDATE_SOCKET_DSNS:
        socket_dir = _probe(dsn)
        if socket_dir is not None:
            return PgTarget(host=socket_dir)
    return None


@contextlib.contextmanager
def _container_target() -> Iterator[PgTarget | None]:
    """A throwaway server in Docker, for a machine that has no other one.

    Last resort, and deliberately so — a container is slower than a running
    server and needs Docker, which is exactly what a CI runner or a developer
    box usually already provides in another form.

    **Pinned to the host client's major version.** The dump/restore tests shell
    out to the host's ``pg_dump``; an 18 client against a 16 server emits
    ``SET transaction_timeout``, a GUC 16 does not have, and ``pg_restore``
    fails with two failures that look real and are not. A matched pair has
    neither problem, so the image follows the client rather than a constant.

    Yields None — never raises and never skips — so ``pg_target`` stays the one
    place that decides what "no server" means. A skip decided here would be the
    #370 failure again, silently.
    """
    if not _client_tools_present():
        yield None
        return
    major = client_major_version()
    if major is None:
        yield None
        return
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        yield None
        return

    try:
        container = PostgresContainer(f"postgres:{major}", driver=None)
        container.start()
    except Exception:
        # No Docker, no image, no network. Not this harness's call to make.
        yield None
        return

    try:
        parsed = urlparse(container.get_connection_url())
        target = PgTarget(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port,
            user=unquote(parsed.username) if parsed.username else None,
            password=unquote(parsed.password) if parsed.password else None,
        )
        # Probed like any other candidate, so "a server with createdb" means the
        # same thing however it was found.
        yield target if _probe(target.dsn(_MAINTENANCE_DB)) is not None else None
    finally:
        container.stop()


def unavailable(reason: str) -> NoReturn:
    """Skip for *reason* — unless this run declared integration mandatory.

    ``FRAISIER_INTEGRATION=1`` is a caller saying a database is supposed to be
    here. A skip then does not mean "inapplicable"; it means the harness failed
    to find what was provided, and that has to be loud. #370 is what a quiet one
    costs: ten preflight tests skipped on every CI run for months — including
    the ``publish.yml`` run that gates the PyPI upload — while the checkmark
    reported a healthy passed count.
    """
    if os.getenv("FRAISIER_INTEGRATION") == "1":
        # ty resolves `pytest.fail` to its deprecated ``(msg, pytrace)``
        # signature and reads the message as the bool, the same mismatch
        # `pytest.skip` hits below.
        loud = f"FRAISIER_INTEGRATION=1 declares a database, but {reason}"
        pytest.fail(loud)  # ty: ignore[invalid-argument-type]
    pytest.skip(reason)  # ty: ignore[too-many-positional-arguments]


@pytest.fixture(scope="session")
def _pg_server() -> Iterator[PgTarget | None]:
    """The server for the whole session: env URL, local socket, or a container.

    Session-scoped because starting a container per test would be absurd, and
    because "is a database reachable here" is one fact, not one per test.
    """
    already_running = _discover_target()
    if already_running is not None:
        yield already_running
        return
    with _container_target() as started:
        yield started


@pytest.fixture
def pg_target(_pg_server: PgTarget | None) -> PgTarget:
    """A PostgreSQL server with createdb privilege, or skip.

    Tests create and drop their own databases on it. Nothing they did not
    create is theirs to drop (#386).
    """
    if _pg_server is None:
        unavailable(
            "no PostgreSQL with createdb privilege is reachable, and no "
            "container could be started"
        )
    return _pg_server


def _hold_flock(
    lock_dir: Path,
    name: str,
    ready: Any,
    release: Any,
) -> None:
    with file_deployment_lock(name, lock_dir=lock_dir):
        ready.set()
        release.wait(timeout=30)


@pytest.fixture
def flock_holder(
    tmp_path,
) -> Iterator[Callable[[str, Path], tuple[Any, Any]]]:
    """Yield a callable that holds a flock on ``<lock_dir>/<name>.lock``.

    Usage::

        proc, release = flock_holder("staging", lock_dir=tmp_path)
        # ... lock is held; do stuff ...
        release.set()
        proc.join(timeout=5)
    """
    ctx = multiprocessing.get_context("spawn")
    spawned: list[tuple[Any, Any]] = []

    def _spawn(name: str, lock_dir: Path) -> tuple[Any, Any]:
        ready = ctx.Event()
        release = ctx.Event()
        proc = ctx.Process(target=_hold_flock, args=(lock_dir, name, ready, release))
        proc.start()
        # Event-based readiness — no time.sleep polling.
        if not ready.wait(timeout=10):
            release.set()
            proc.join(timeout=5)
            msg = "flock holder did not signal ready"
            raise RuntimeError(msg)
        spawned.append((proc, release))
        return proc, release

    yield _spawn

    for proc, release in spawned:
        release.set()
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2)

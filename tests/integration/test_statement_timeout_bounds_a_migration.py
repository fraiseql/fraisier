"""A blocked migration is ended by the server, on a real server (#388).

#384 established the limit `timeout:` could not cross: the deploy timer raises
with `PyThreadState_SetAsyncExc`, which lands at the next bytecode boundary, and
a thread inside libpq reaches none until the wait returns. A migration blocked
in the database therefore held the per-fraise lock and the `deploying` record
for as long as it blocked.

PostgreSQL's own `statement_timeout` can end it, and libpq accepts the GUC
through the connection string — so this needed nothing from confiture. The unit
tests assert the URL fraisier builds. This one connects with it and blocks, and
is the reason the argv assertions are allowed to stand: when a test asserts on a
connection string, a second test connects with it.
"""

from __future__ import annotations

import time

import pytest

from fraisier.timeout import deployment_timeout, statement_timeout_url

psycopg = pytest.importorskip("psycopg")


def test_the_url_fraisier_builds_actually_bounds_a_blocking_statement(pg_target):
    """`pg_sleep` is the cleanest stand-in for a migration waiting on a lock:
    it blocks inside libpq exactly the way one does."""
    url = statement_timeout_url(pg_target.dsn("postgres"), seconds=0.3)
    assert url is not None

    started = time.monotonic()
    with (
        psycopg.connect(url) as conn,
        pytest.raises(psycopg.errors.QueryCanceled),
        conn.cursor() as cur,
    ):
        cur.execute("SELECT pg_sleep(10)")

    elapsed = time.monotonic() - started
    assert elapsed < 9, f"the statement ran for {elapsed:.1f}s despite the bound"


def test_the_guc_reaches_the_session(pg_target):
    url = statement_timeout_url(pg_target.dsn("postgres"), seconds=7)

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        assert cur.fetchone()[0] == "7s"


def test_an_operator_supplied_option_survives_alongside_it(pg_target):
    """fraisier appends to `options` rather than replacing it; both GUCs must
    reach the session, or an operator's own setting is silently dropped."""
    base = pg_target.dsn("postgres")
    joiner = "&" if "?" in base else "?"
    with_lock_timeout = f"{base}{joiner}options=-c%20lock_timeout%3D4000"

    url = statement_timeout_url(with_lock_timeout, seconds=9)

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SHOW lock_timeout")
        assert cur.fetchone()[0] == "4s"
        cur.execute("SHOW statement_timeout")
        assert cur.fetchone()[0] == "9s"


def test_a_deploy_budget_becomes_the_server_side_bound(pg_target):
    """End to end: the budget the deploy is running under is what the server
    enforces, without anything having to interrupt a Python thread."""
    with deployment_timeout(60):
        from fraisier.timeout import remaining_budget

        remaining = remaining_budget()
        assert remaining is not None
        url = statement_timeout_url(pg_target.dsn("postgres"), seconds=remaining)

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_setting('statement_timeout')::interval")
        applied = cur.fetchone()[0].total_seconds()

    # ~60s, allowing for the moments spent building the URL. PostgreSQL renders
    # the GUC in whichever unit is exact, so compare the interval, not the text.
    assert 55 <= applied <= 60, applied

"""`timeout:` reaches the thread that is deploying, and the database (#388).

Follow-up to #384, which bounded what could be bounded and wrote down the rest.
Two things it left:

1. **The timer injected into the wrong thread.** `_interrupt_main_thread` asks
   for `threading.main_thread()`. A deploy dispatched through the webhook's
   `BackgroundTasks` does not necessarily run there — so the exception landed in
   the process's main thread, which is uvicorn's event loop, while the deploy
   carried on.
2. **A migration blocked in the database was unbounded.** The commonest hang on
   the deploy path, and the one `timeout:` could not touch: the wait is inside
   libpq, and `PyThreadState_SetAsyncExc` raises at the next bytecode boundary.
   PostgreSQL's own `statement_timeout` can end it, and libpq takes one through
   the connection URL — no confiture API needed.
"""

from __future__ import annotations

import threading
import time

import pytest

from fraisier.timeout import (
    DeploymentTimeoutExpired,
    deployment_timeout,
    statement_timeout_url,
)


class TestTheTimerFindsTheDeployingThread:
    def test_it_raises_in_the_thread_that_entered_it(self):
        """The whole point: a deploy on a worker thread must be the thread that
        gets the exception."""
        caught: list[str] = []

        def _deploy_on_a_worker():
            try:
                with deployment_timeout(0.1):
                    time.sleep(1.0)
            except DeploymentTimeoutExpired:
                caught.append("worker")

        thread = threading.Thread(target=_deploy_on_a_worker)
        thread.start()
        thread.join(timeout=5)

        assert caught == ["worker"], (
            "the timeout did not reach the thread that was deploying"
        )

    def test_the_main_thread_is_left_alone(self):
        """Injecting into `main_thread()` from a worker deploy would hit
        uvicorn's event loop — a thread that has no idea what a deployment
        timeout is."""
        main_thread_saw: list[BaseException] = []
        done = threading.Event()

        def _deploy_on_a_worker():
            try:
                with deployment_timeout(0.1):
                    time.sleep(0.5)
            except DeploymentTimeoutExpired:
                pass
            finally:
                done.set()

        thread = threading.Thread(target=_deploy_on_a_worker)
        thread.start()
        try:
            while not done.is_set():
                time.sleep(0.02)
        except BaseException as exc:  # the main thread must never see it
            main_thread_saw.append(exc)
        thread.join(timeout=5)

        assert main_thread_saw == []

    def test_it_still_works_on_the_main_thread(self):
        with pytest.raises(DeploymentTimeoutExpired), deployment_timeout(0.1):
            time.sleep(1.0)


def _bounded(url: str, seconds: float) -> str:
    """`statement_timeout_url` narrowed to str — it returns None only for a
    None input, which has its own test."""
    result = statement_timeout_url(url, seconds=seconds)
    assert result is not None
    return result


class TestStatementTimeoutUrl:
    def test_it_adds_the_option_to_a_bare_url(self):
        url = _bounded("postgresql://h/db", 30)

        assert "statement_timeout" in url
        assert "30000" in url

    def test_it_preserves_existing_query_parameters(self):
        url = _bounded("postgresql://h/db?host=/run/postgresql&sslmode=require", 5)

        assert "host=%2Frun%2Fpostgresql" in url or "host=/run/postgresql" in url
        assert "sslmode=require" in url
        assert "statement_timeout" in url

    def test_it_does_not_clobber_an_operator_supplied_options(self):
        """An operator who set `options` meant it; fraisier appends rather than
        replaces, and PostgreSQL takes the last setting of a GUC."""
        url = _bounded("postgresql://h/db?options=-c%20lock_timeout%3D1000", 5)

        assert "lock_timeout" in url
        assert "statement_timeout" in url

    def test_a_none_url_stays_none(self):
        """confiture resolves its own URL when fraisier supplies none; there is
        nothing to add the option to."""
        assert statement_timeout_url(None, seconds=30) is None

    def test_a_non_positive_budget_leaves_the_url_alone(self):
        assert statement_timeout_url("postgresql://h/db", seconds=0) == (
            "postgresql://h/db"
        )

    def test_an_unparseable_url_is_returned_unchanged(self):
        """Never break a connection string to add a diagnostic bound."""
        assert statement_timeout_url("not a url at all", seconds=5) == (
            "not a url at all"
        )

    def test_a_socket_style_url_survives_intact(self):
        """`postgresql:///db?host=/run/postgresql` — an empty netloc — is the
        shape this project uses everywhere. Round-tripping it through
        `urlunsplit` collapses it to `postgresql:/db`, which libpq rejects."""
        url = _bounded("postgresql:///mydb?host=/run/postgresql", 5)

        assert url.startswith("postgresql:///mydb?"), url
        assert "statement_timeout" in url

    def test_a_socket_style_url_with_no_query_survives_intact(self):
        url = _bounded("postgresql:///mydb", 5)

        assert url.startswith("postgresql:///mydb?"), url

    def test_the_space_is_percent_encoded_not_a_plus(self):
        """libpq percent-decodes a connection string but does not read "+" as
        a space, so `quote_plus` encoding yields `-c+statement_timeout=…` and
        the server rejects `+statement_timeout` as an unknown parameter."""
        url = _bounded("postgresql:///mydb", 5)

        assert "%20" in url
        assert "+" not in url

    def test_the_option_is_milliseconds(self):
        url = _bounded("postgresql://h/db", 1.5)

        assert "1500" in url


class TestTheMigrationConnectionIsBounded:
    """The deploy hands confiture a URL carrying the deploy's own budget.

    A migration that blocks in the database now ends when the deploy's time
    does, instead of holding the per-fraise lock and the `deploying` record for
    as long as it blocks.
    """

    def _deployer(self, **db):
        from fraisier.deployers.api import APIDeployer

        return APIDeployer(
            {
                "fraise_name": "my_api",
                "environment": "production",
                "app_path": "/var/www/api",
                "database": {
                    "strategy": "migrate",
                    "name": "mydb",
                    "database_url": "postgresql:///mydb",
                    **db,
                },
            }
        )

    def test_the_url_carries_the_remaining_budget(self):
        deployer = self._deployer()

        with deployment_timeout(30):
            _s, _c, _m, url = deployer._resolve_strategy()

        assert url is not None
        assert "statement_timeout" in url
        # Derived from the budget, not a literal: ~30s, in milliseconds.
        assert "29" in url or "30000" in url

    def test_outside_a_deploy_the_url_is_untouched(self):
        """A CLI-driven migration has no deploy budget to inherit."""
        deployer = self._deployer()

        _s, _c, _m, url = deployer._resolve_strategy()

        assert url == "postgresql:///mydb"

    def test_an_operator_can_turn_it_off(self):
        deployer = self._deployer(statement_timeout=False)

        with deployment_timeout(30):
            _s, _c, _m, url = deployer._resolve_strategy()

        assert url == "postgresql:///mydb"

    def test_an_operator_can_pin_a_value(self):
        deployer = self._deployer(statement_timeout=7)

        with deployment_timeout(600):
            _s, _c, _m, url = deployer._resolve_strategy()

        assert "7000" in url

    def test_no_url_stays_no_url(self):
        """confiture resolves its own; nothing to attach to."""
        deployer = self._deployer(database_url=None)

        with deployment_timeout(30):
            _s, _c, _m, url = deployer._resolve_strategy()

        assert url is None

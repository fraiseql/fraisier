"""The restore pipeline refuses to start the service on an empty read model (#422).

It runs after every step that can change the database (restore, rebuild,
migrate) and before the receipt — which means "this run completed" — and before
the service starts, so a failure leaves nothing serving an empty TVIEW.

``tests/conftest.py`` stubs this step for every other test that drives the
pipeline to completion; this module is the exception.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from fraisier.config.schema import PreflightConfig
from fraisier.dbops.tviews import EmptyTview, TviewError
from fraisier.errors import DatabaseError
from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy
from tests.test_restore_actuation_receipt import _execute

_ADMIN_URL = "postgresql://postgres@localhost:5432/postgres"
EMPTY = [EmptyTview("public", "tv_post", "public", "v_post")]


def _strategy(on_empty_tview: str = "fail") -> RestoreMigrateStrategy:
    return RestoreMigrateStrategy(
        RestoreConfig(
            db_name="staging_db",
            backup_dir=Path("/backup"),
            preflight=PreflightConfig(enabled=False),
            on_empty_tview=on_empty_tview,
        ),
        admin_url=_ADMIN_URL,
    )


def _check(strategy: RestoreMigrateStrategy, *, found=(), raises=None) -> str:
    with patch(
        "fraisier.dbops.tviews.find_empty_tviews",
        side_effect=raises,
        return_value=list(found),
    ) as find:
        strategy._check_tviews_not_empty()
    return find.call_args.args[0] if find.call_args else ""


def test_an_empty_tview_refuses_the_restore_and_names_the_pair() -> None:
    with pytest.raises(DatabaseError, match=r"public\.tv_post.*public\.v_post"):
        _check(_strategy(), found=EMPTY)


@pytest.mark.parametrize("mode", ["fail", "warn"])
def test_the_restore_passes_its_on_empty_choice_for_an_unreadable_tview(
    mode: str,
) -> None:
    strategy = _strategy(on_empty_tview=mode)
    with patch("fraisier.dbops.tviews.find_empty_tviews", return_value=[]) as find:
        strategy._check_tviews_not_empty()

    assert find.call_args.kwargs["on_unreadable"] == mode


def test_it_probes_the_restored_database_not_the_maintenance_one() -> None:
    url = _check(_strategy())

    assert url.endswith("/staging_db")


def test_warn_logs_and_lets_the_restore_finish(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        _check(_strategy(on_empty_tview="warn"), found=EMPTY)

    assert "public.tv_post" in caplog.text


def test_a_probe_that_cannot_run_refuses_under_fail() -> None:
    with pytest.raises(DatabaseError, match="could not check"):
        _check(_strategy(), raises=TviewError("pg_tviews here predates contract 1"))


def test_it_runs_after_the_migration_and_before_the_receipt() -> None:
    order: list[str] = []
    with patch(
        "fraisier.dbops.tviews.find_empty_tviews",
        side_effect=lambda *_a, **_k: order.append("tviews") or [],
    ):
        _execute(order=order)

    assert order == ["restore", "migrate", "tviews", "receipt"]

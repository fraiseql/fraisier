"""``fraisier db tviews status|rebuild`` — the failover runbook (#422).

fraisier does not drive a failover or a crash restart, so what follows one is an
operator command: promote, then ``db tviews rebuild``.  ``status`` is what to look
at first, and what a monitoring timer can call: exit 0 healthy, 1 a TVIEW is empty
under a populated view, 3 could not check — never 0 for "could not look".
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.dbops.tviews import EmptyTview, TviewError, TviewRebuilt
from fraisier.errors import DeploymentLockError

URL = "postgresql://app@db.example/app"
EMPTY = [EmptyTview("public", "tv_post", "public", "v_post")]
PROFILE = [{"entity": "post", "tview": "tv_post", "persistence": "unlogged"}]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def cfg():
    config = MagicMock()
    config.get_fraise.return_value = {"type": "api"}
    config.get_fraise_environment.return_value = {
        "type": "api",
        "database": {"name": "app", "database_url": URL},
    }
    config._config = {}
    with patch("fraisier.cli.main.get_config", return_value=config):
        yield config


def _status(
    runner, *args, installed=True, empty=(), profile=PROFILE, empty_raises=None
):
    with (
        patch("fraisier.dbops.tviews.tviews_installed", return_value=installed),
        patch("fraisier.dbops.tviews.profile_tviews", return_value=profile),
        patch(
            "fraisier.dbops.tviews.find_empty_tviews",
            return_value=list(empty),
            side_effect=empty_raises,
        ),
    ):
        return runner.invoke(
            main, ["db", "tviews", "status", "api", "-e", "prod", *args]
        )


class TestStatus:
    def test_healthy_exits_zero_and_lists_each_tview(self, runner, cfg) -> None:
        result = _status(runner)

        assert result.exit_code == 0, result.output
        assert "tv_post" in result.output

    def test_an_empty_tview_exits_one_and_names_the_pair(self, runner, cfg) -> None:
        result = _status(runner, empty=EMPTY)

        assert result.exit_code == 1
        assert "public.tv_post" in result.output
        assert "public.v_post" in result.output

    def test_no_pg_tviews_is_not_a_pass(self, runner, cfg) -> None:
        result = _status(runner, installed=False)

        assert result.exit_code == 3
        assert "not installed" in result.output

    def test_an_unreachable_database_is_not_a_pass(self, runner, cfg) -> None:
        with patch(
            "fraisier.dbops.tviews.tviews_installed",
            side_effect=psycopg.OperationalError("refused"),
        ):
            result = runner.invoke(
                main, ["db", "tviews", "status", "api", "-e", "prod"]
            )

        assert result.exit_code == 3
        assert "refused" in result.output

    def test_a_standby_still_gets_its_profile(self, runner, cfg) -> None:
        """An UNLOGGED TVIEW cannot be read on a standby, so emptiness is unknown."""
        result = _status(
            runner,
            empty_raises=psycopg.errors.FeatureNotSupported("unlogged in recovery"),
        )

        assert result.exit_code == 0
        assert "tv_post" in result.output
        assert "emptiness" in result.output.lower()

    def test_json_carries_the_profile_and_the_empty_pairs(self, runner, cfg) -> None:
        result = _status(runner, "--json", empty=EMPTY)

        payload = json.loads(result.output)
        assert payload["tviews"] == PROFILE
        assert payload["empty"] == [
            {"tview": "public.tv_post", "view": "public.v_post"}
        ]
        assert payload["emptiness_checked"] is True
        assert result.exit_code == 1

    def test_an_unknown_fraise_fails(self, runner, cfg) -> None:
        cfg.get_fraise.return_value = None

        result = runner.invoke(main, ["db", "tviews", "status", "nope", "-e", "prod"])

        assert result.exit_code == 1


def _rebuild(runner, *args, rebuilt=(), raises=None, lock=None):
    with (
        patch(
            "fraisier.dbops.tviews.rebuild_empty_tviews", return_value=list(rebuilt)
        ) as empty,
        patch(
            "fraisier.dbops.tviews.rebuild_all_tviews", return_value=list(rebuilt)
        ) as everything,
        patch("fraisier.dbops.tviews.tviews_installed", return_value=True),
        patch("fraisier.locking.deployment_lock", side_effect=lock) as taken,
    ):
        if raises is not None:
            empty.side_effect = raises
            everything.side_effect = raises
        result = runner.invoke(
            main, ["db", "tviews", "rebuild", "api", "-e", "prod", *args]
        )
    return result, empty, everything, taken


class TestRebuild:
    def test_it_rebuilds_the_empty_ones_and_reports_each(self, runner, cfg) -> None:
        result, empty, everything, _ = _rebuild(
            runner, rebuilt=[TviewRebuilt("post", 3)]
        )

        assert result.exit_code == 0, result.output
        assert "post" in result.output
        assert "3" in result.output
        empty.assert_called_once_with(URL)
        everything.assert_not_called()

    def test_all_rebuilds_every_tview(self, runner, cfg) -> None:
        _, empty, everything, _ = _rebuild(runner, "--all")

        everything.assert_called_once_with(URL)
        empty.assert_not_called()

    def test_nothing_to_do_says_so(self, runner, cfg) -> None:
        result, *_ = _rebuild(runner)

        assert result.exit_code == 0
        assert "nothing" in result.output.lower()

    def test_a_standby_is_refused_plainly_and_exits_nonzero(self, runner, cfg) -> None:
        result, *_ = _rebuild(
            runner, raises=TviewError("the database is in recovery; run on the primary")
        )

        assert result.exit_code == 1
        assert "recovery" in result.output
        assert "primary" in result.output

    def test_it_holds_the_deployment_lock(self, runner, cfg) -> None:
        *_, taken = _rebuild(runner)

        taken.assert_called_once_with("api")

    def test_a_held_lock_fails_unless_told_to_skip(self, runner, cfg) -> None:
        result, empty, *_ = _rebuild(runner, lock=DeploymentLockError("busy"))

        assert result.exit_code == 1
        empty.assert_not_called()

    def test_skip_if_locked_exits_zero(self, runner, cfg) -> None:
        result, *_ = _rebuild(
            runner, "--skip-if-locked", lock=DeploymentLockError("busy")
        )

        assert result.exit_code == 0
        assert "Skipping" in result.output


def test_both_commands_are_documented_with_examples(runner) -> None:
    for command in ("status", "rebuild"):
        out = runner.invoke(main, ["db", "tviews", command, "--help"]).output
        assert out.count("fraisier db tviews") >= 2

"""The pgBackRest restore source: the sequence, and every way it fails closed (#424).

A physical restore replaces a whole cluster, so the interesting tests are the
refusals. The rule they pin: once the cluster has been stopped for a restore,
**any** failure leaves it stopped and the application service not started —
because what is on disk is, at that point, a half-restored production copy that
could be archiving into production's own repository.

The helper is a fake that records its calls; the Docker integration test runs the
same source against a real helper, pgBackRest and two real clusters.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fraisier.config.restore_source import PgBackRestSpec
from fraisier.dbops.pgbackrest import HelperUnavailableError
from fraisier.dbops.tviews import TviewError, TviewRebuilt
from fraisier.errors import DatabaseError, RestoreFailedClosed
from fraisier.strategies import RestoreConfig, RestoreMigrateStrategy
from fraisier.strategies._restore_sources import PgBackRestSource

FIXTURES = Path(__file__).parent / "fixtures" / "pgbackrest"
INFO = (FIXTURES / "info-full-and-incrs.json").read_text()
NEWEST = "20261001-141744F_20261001-141820I"
ADMIN_URL = "postgresql://postgres@localhost:5433/postgres"
DATADIR = "/var/lib/postgresql/18/staging"
SUMMARY = {
    "label": NEWEST,
    "restore_size": "30.3MB",
    "files_total": 1295,
    "files_rewritten": 7,
    "bytes_rewritten": 73728,
    "bytes_rewritten_is_estimate": True,
}
#: "now", shortly after the newest captured backup stopped (epoch 1790864301).
NOW = datetime.fromtimestamp(1790864301, UTC) + timedelta(hours=2)


class FakeHelper:
    """Stands in for ``HelperClient``: records calls, fails where told to."""

    def __init__(
        self,
        *,
        info: str = INFO,
        fail: dict[str, str] | None = None,
        signals: list[str] | None = None,
        order: list[str] | None = None,
        online: bool = False,
        datadir: str = DATADIR,
        status_reply: dict[str, Any] | None = None,
    ) -> None:
        self.info_text = info
        self.fail = fail or {}
        self.signals = signals or []
        #: whether ``status`` finds the cluster up (it is down for a re-run after a
        #: fail-closed refresh, and then there is nothing to ask it)
        self.online = online
        self.datadir = datadir
        self.status_reply = status_reply
        #: raised from ``restore``, to model an interruption (a deploy timeout, ^C)
        self.interrupt_with: BaseException | None = None
        self.calls: list[str] = []
        #: a list shared with the test, to pin the order of calls across objects
        self.order = order if order is not None else []

    def _record(self, call: str) -> None:
        self.calls.append(call)
        self.order.append(call)

    def _do(self, action: str, reply: dict[str, Any]) -> dict[str, Any]:
        self._record(action)
        if action in self.fail:
            raise HelperUnavailableError(self.fail[action])
        return reply

    def info(self) -> str:
        self._record("info")
        if "info" in self.fail:
            raise HelperUnavailableError(self.fail["info"])
        return self.info_text

    def stop(self) -> dict[str, Any]:
        return self._do("stop", {"ok": True})

    def restore(self, label: str) -> dict[str, Any]:
        self._record(f"restore:{label}")
        if self.interrupt_with is not None:
            raise self.interrupt_with
        if "restore" in self.fail:
            raise HelperUnavailableError(self.fail["restore"])
        return {"ok": True, "summary": SUMMARY, "tail": ""}

    def start(self) -> dict[str, Any]:
        return self._do("start", {"ok": True})

    def status(self) -> dict[str, Any]:
        reply = self.status_reply or {
            "ok": True,
            "online": self.online,
            "signals": self.signals,
            "datadir": self.datadir,
        }
        return self._do("status", reply)


class World:
    """Everything the source talks to besides the helper, patched at one place."""

    def __init__(
        self,
        *,
        archive_mode: str = "off",
        reset_fails: Exception | None = None,
        database_present: bool = True,
        tviews: list[TviewRebuilt] | None = None,
        promote_fails: Exception | None = None,
        residue: dict[str, str] | None = None,
        data_directory: str = DATADIR,
    ) -> None:
        self.data_directory = data_directory
        self.archive_mode = archive_mode
        self.reset_fails = reset_fails
        self.database_present = database_present
        self.tviews = tviews
        self.promote_fails = promote_fails
        self.residue = residue or {"restore_command": "", "primary_conninfo": ""}
        self.events: list[str] = []

    def __enter__(self) -> World:
        from contextlib import ExitStack

        self.stack = ExitStack()
        mod = "fraisier.dbops.pgbackrest"
        self.stack.enter_context(
            patch(f"{mod}.wait_until_promoted", side_effect=self._promote)
        )
        self.stack.enter_context(
            patch(
                f"{mod}.read_setting",
                side_effect=lambda _url, name: self._setting(name),
            )
        )
        self.stack.enter_context(
            patch(f"{mod}.reset_replication_settings", side_effect=self._reset)
        )
        self.stack.enter_context(
            patch(
                f"{mod}.database_exists", side_effect=lambda *_a: self.database_present
            )
        )
        self.stack.enter_context(
            patch(
                "fraisier.dbops.tviews.tviews_installed",
                return_value=self.tviews is not None,
            )
        )
        self.stack.enter_context(
            patch(
                "fraisier.dbops.tviews.rebuild_empty_tviews",
                side_effect=lambda _url: self.tviews or [],
            )
        )
        return self

    def __exit__(self, *exc: object) -> None:
        self.stack.close()

    def _promote(self, *_a: Any, **_k: Any) -> None:
        self.events.append("promoted")
        if self.promote_fails:
            raise self.promote_fails

    def _setting(self, name: str) -> str:
        self.events.append(f"read:{name}")
        if name == "data_directory":
            return self.data_directory
        if name == "archive_mode":
            return self.archive_mode
        return self.residue.get(name, "")

    def _reset(self, *_a: Any, **_k: Any) -> dict[str, str]:
        self.events.append("reset")
        if self.reset_fails:
            raise self.reset_fails
        return self.residue


def spec(**overrides: Any) -> PgBackRestSpec:
    base = PgBackRestSpec(stanza="main", repo=1, cluster="18/staging")
    return dataclasses.replace(base, **overrides)


def build(
    helper: FakeHelper,
    *,
    config_spec: PgBackRestSpec | None = None,
    owner: str | None = None,
    max_age_hours: float = 48.0,
) -> tuple[RestoreMigrateStrategy, PgBackRestSource, MagicMock]:
    config = RestoreConfig(
        db_name="app",
        backup_dir=Path("/unused"),
        max_age_hours=max_age_hours,
        target_owner=owner,
        pgbackrest=config_spec or spec(),
        pgbackrest_socket="/run/fraisier/x.sock",
    )
    service = MagicMock()
    source = PgBackRestSource(config, client=helper, now=lambda: NOW)
    strategy = RestoreMigrateStrategy(
        config,
        admin_url=ADMIN_URL,
        service_manager=service,
        service_name="api.service",
        source=source,
    )
    return strategy, source, service


def prepare(
    strategy: RestoreMigrateStrategy, source: PgBackRestSource, skip: bool = False
):
    return source.prepare(
        strategy,
        confiture_config=Path("c.yaml"),
        migrations_dir=Path("m"),
        skip_preflight=skip,
    )


class TestPrepare:
    def test_it_chooses_the_latest_backup_and_names_it_as_the_backup_ref(self) -> None:
        helper = FakeHelper()
        strategy, source, _ = build(helper)

        prepared = prepare(strategy, source)

        assert prepared.backup_ref == f"pgbackrest:main/{NEWEST}"
        assert prepared.backup_bytes == 31779757
        assert prepared.archive_check is None
        assert helper.calls == ["info", "status"]

    def test_nothing_is_touched_while_it_chooses(self) -> None:
        helper = FakeHelper()
        strategy, source, service = build(helper)

        prepare(strategy, source)

        service.stop.assert_not_called()
        assert "stop" not in helper.calls

    def test_a_backup_older_than_max_age_is_refused_before_anything_is_stopped(
        self,
    ) -> None:
        strategy, source, service = build(FakeHelper(), max_age_hours=1)

        with pytest.raises(DatabaseError, match=rf"{NEWEST}.*older than 1"):
            prepare(strategy, source)

        service.stop.assert_not_called()

    def test_a_point_in_time_target_is_not_aged_out(self) -> None:
        """An old instant is the point of a PITR refresh, not a stale backup."""
        strategy, source, _ = build(
            FakeHelper(),
            config_spec=spec(target="2026-10-01 14:17:46.5+00"),
            max_age_hours=1,
        )

        assert (
            prepare(strategy, source).backup_ref == "pgbackrest:main/20261001-141744F"
        )

    def test_an_unreachable_helper_is_a_database_error_before_any_stop(self) -> None:
        strategy, source, service = build(FakeHelper(fail={"info": "not reachable"}))

        with pytest.raises(DatabaseError, match="not reachable"):
            prepare(strategy, source)

        service.stop.assert_not_called()

    def test_a_missing_stanza_is_a_database_error(self) -> None:
        unknown = (FIXTURES / "info-unknown-stanza.json").read_text()
        strategy, source, _ = build(
            FakeHelper(info=unknown), config_spec=spec(stanza="nope")
        )

        with pytest.raises(DatabaseError, match="missing stanza path"):
            prepare(strategy, source)

    def test_the_migration_preflight_is_skipped_and_says_so(self, caplog) -> None:
        """Preflight reads a pg_dump archive; a physical restore has none."""
        strategy, source, _ = build(FakeHelper())
        with (
            caplog.at_level("INFO"),
            patch.object(strategy, "_run_preflight") as preflight,
        ):
            prepare(strategy, source)

        preflight.assert_not_called()
        assert "preflight" in caplog.text.lower()
        assert "skipped" in caplog.text.lower()

    def test_the_log_names_label_kind_stop_time_and_size(self, caplog) -> None:
        strategy, source, _ = build(FakeHelper())

        with caplog.at_level("INFO"):
            prepare(strategy, source)

        assert NEWEST in caplog.text
        assert "incr" in caplog.text
        assert "2026-10-01" in caplog.text


class TestRestore:
    def run(
        self, helper: FakeHelper | None = None, world: World | None = None, **kw: Any
    ):
        helper = helper or FakeHelper()
        world = world or World()
        strategy, source, service = build(helper, **kw)
        prepared = prepare(strategy, source)
        with world:
            restored = source.restore(strategy, prepared)
        return restored, helper, world, service

    def test_the_sequence_is_service_cluster_restore_start_promote_reset(self) -> None:
        order: list[str] = []
        helper = FakeHelper(order=order)
        strategy, source, service = build(helper)
        service.stop.side_effect = lambda *_a: order.append("service-stop")
        prepared = prepare(strategy, source)
        order.clear()
        world = World()
        with world:
            source.restore(strategy, prepared)

        assert order == [
            "service-stop",
            "stop",
            f"restore:{NEWEST}",
            "start",
            "status",
        ]
        assert [e for e in world.events if not e.startswith("read:")] == [
            "promoted",
            "reset",
        ]

    def test_the_helper_is_told_the_label_that_was_validated(self) -> None:
        _, helper, _, _ = self.run()

        assert f"restore:{NEWEST}" in helper.calls

    def test_it_reports_the_phases_for_the_deploy_log_and_metrics(self) -> None:
        restored, _, _, _ = self.run()

        assert set(restored.phases) == {
            "pgbackrest_restore",
            "cluster_start",
            "recovery",
            "safety",
        }
        assert restored.schema_floor is None  # no archive TOC to state one
        assert restored.restore_secs >= 0

    def test_the_restore_summary_reaches_the_log(self, caplog) -> None:
        with caplog.at_level("INFO"):
            self.run()

        assert "7 of 1295" in caplog.text
        assert NEWEST in caplog.text

    def test_the_rebuilt_tviews_are_reported_like_the_dump_path_reports_them(
        self, caplog
    ) -> None:
        with caplog.at_level("INFO"):
            self.run(world=World(tviews=[TviewRebuilt("post", 3)]))

        assert "pg_tviews: rebuilt 1 empty TVIEW(s) post=3rows" in caplog.text

    def test_ownership_is_reassigned_when_asked(self) -> None:
        with patch(
            "fraisier.dbops.restore._reassign_owner", return_value=(0, "", "")
        ) as reassign:
            self.run(owner="app_owner")

        assert reassign.call_args.args[:2] == ("app", "app_owner")

    def test_the_database_name_must_exist_in_the_restored_cluster(self) -> None:
        """The restored cluster carries production's databases, under production's names."""
        with pytest.raises(RestoreFailedClosed, match=r"database\.name"):
            self.run(world=World(database_present=False))


class TestTheCheckedClusterIsTheRestoredOne:
    """``admin_url`` comes from ``fraises.yaml``; the restored cluster comes from the helper.

    The safety checks — and ``ALTER SYSTEM RESET`` — run against whatever ``admin_url``
    reaches. If that is not the cluster that was restored, ``archive_mode`` is read
    from the wrong server and a false pass is the best case; the worst is a reset
    against production. So the data directory behind ``admin_url`` must be the one the
    helper restored.
    """

    def test_a_different_cluster_behind_the_url_is_refused_before_anything_is_stopped(
        self,
    ) -> None:
        strategy, source, service = build(FakeHelper(online=True))

        with (
            World(data_directory="/var/lib/postgresql/18/prod") as world,
            pytest.raises(DatabaseError, match=r"/var/lib/postgresql/18/prod"),
        ):
            prepare(strategy, source)

        service.stop.assert_not_called()
        assert "reset" not in world.events

    def test_the_matching_cluster_passes(self) -> None:
        strategy, source, _ = build(FakeHelper(online=True))

        with World():
            assert prepare(strategy, source).backup_label == NEWEST

    def test_a_stopped_cluster_cannot_be_asked_so_prepare_defers_to_the_restore(
        self,
    ) -> None:
        """The re-run after a fail-closed refresh: the cluster is down on purpose."""
        strategy, source, _ = build(FakeHelper(online=False))

        with World() as world:
            prepare(strategy, source)

        assert "read:data_directory" not in world.events

    def test_the_restored_cluster_is_checked_again_before_anything_is_changed(
        self,
    ) -> None:
        """Mandatory, and before the first ``ALTER SYSTEM``."""
        strategy, source, _ = build(FakeHelper())
        prepared = prepare(strategy, source)

        with (
            World(data_directory="/srv/other") as world,
            pytest.raises(RestoreFailedClosed, match="/srv/other"),
        ):
            source.restore(strategy, prepared)

        assert "reset" not in world.events
        assert "read:archive_mode" not in world.events

    def test_a_status_without_a_data_directory_cannot_prove_it_and_fails_closed(
        self,
    ) -> None:
        helper = FakeHelper(status_reply={"ok": True, "online": False, "signals": []})
        strategy, source, _ = build(helper)
        prepared = prepare(strategy, source)

        with World(), pytest.raises(RestoreFailedClosed, match="data directory"):
            source.restore(strategy, prepared)


class TestAMalformedReplyFailsClosed:
    def test_signals_that_are_not_a_list_cannot_prove_the_directory_is_clean(
        self,
    ) -> None:
        helper = FakeHelper(
            status_reply={
                "ok": True,
                "online": False,
                "signals": "none",
                "datadir": DATADIR,
            }
        )
        strategy, source, _ = build(helper)
        prepared = prepare(strategy, source)

        with World(), pytest.raises(RestoreFailedClosed, match="signals"):
            source.restore(strategy, prepared)


class TestAnInterruptionIsAlsoAFailure:
    """A deploy's ``timeout:`` lands asynchronously, as an exception, wherever the code is.

    If it escaped, the failed deploy's handler would restart the service against a
    stopped, half-restored cluster. So once the cluster is stopped, *anything* that
    interrupts the refresh fails closed.
    """

    def test_a_timeout_exception_inside_the_refresh_fails_closed(self) -> None:
        from fraisier.timeout import DeploymentTimeoutExpired

        helper = FakeHelper()
        strategy, source, service = build(helper)
        prepared = prepare(strategy, source)

        helper.interrupt_with = DeploymentTimeoutExpired("deploy exceeded 600s")
        with World(), pytest.raises(RestoreFailedClosed, match="exceeded 600s"):
            source.restore(strategy, prepared)

        assert helper.calls[-1] == "stop"
        service.start.assert_not_called()

    def test_a_keyboard_interrupt_fails_closed_too(self) -> None:
        helper = FakeHelper()
        strategy, source, _ = build(helper)
        prepared = prepare(strategy, source)

        helper.interrupt_with = KeyboardInterrupt()
        with World(), pytest.raises(RestoreFailedClosed):
            source.restore(strategy, prepared)

        assert helper.calls[-1] == "stop"


class TestFailingClosed:
    """After the cluster is stopped, nothing may leave it running or the service starting."""

    def attempt(
        self, helper: FakeHelper, world: World
    ) -> tuple[Exception, FakeHelper, MagicMock]:
        strategy, source, service = build(helper)
        prepared = prepare(strategy, source)
        with world, pytest.raises(DatabaseError) as raised:
            source.restore(strategy, prepared)
        return raised.value, helper, service

    def test_archive_mode_on_stops_the_cluster_again_and_says_why(self) -> None:
        """The restored cluster must never archive into the source stanza.

        Mutation: delete the `archive_mode` check and this test fails.
        """
        helper = FakeHelper()
        error, helper, service = self.attempt(helper, World(archive_mode="on"))

        assert isinstance(error, RestoreFailedClosed)
        assert "archive_mode" in str(error)
        assert helper.calls[-1] == "stop"  # stopped again after the check
        assert helper.calls.count("stop") == 2
        service.start.assert_not_called()

    @pytest.mark.parametrize("mode", ["always", "ON", "on"])
    def test_anything_but_off_fails(self, mode: str) -> None:
        error, _, _ = self.attempt(FakeHelper(), World(archive_mode=mode))

        assert isinstance(error, RestoreFailedClosed)

    def test_a_recovery_signal_left_behind_fails_closed(self) -> None:
        error, helper, service = self.attempt(
            FakeHelper(signals=["recovery.signal"]), World()
        )

        assert isinstance(error, RestoreFailedClosed)
        assert "recovery.signal" in str(error)
        assert helper.calls[-1] == "stop"
        service.start.assert_not_called()

    def test_a_standby_signal_fails_closed_too(self) -> None:
        error, _, _ = self.attempt(FakeHelper(signals=["standby.signal"]), World())

        assert "standby.signal" in str(error)

    def test_replication_settings_that_survive_the_reset_fail_closed(self) -> None:
        residue = {
            "restore_command": "pgbackrest archive-get %f %p",
            "primary_conninfo": "",
        }

        error, helper, _ = self.attempt(FakeHelper(), World(residue=residue))

        assert isinstance(error, RestoreFailedClosed)
        assert "restore_command" in str(error)
        assert helper.calls[-1] == "stop"

    def test_a_failed_reset_fails_closed(self) -> None:
        error, helper, _ = self.attempt(
            FakeHelper(), World(reset_fails=RuntimeError("permission denied"))
        )

        assert isinstance(error, RestoreFailedClosed)
        assert helper.calls[-1] == "stop"

    def test_a_failed_pgbackrest_restore_leaves_the_cluster_stopped(self) -> None:
        error, helper, service = self.attempt(
            FakeHelper(fail={"restore": "unable to find backup set"}), World()
        )

        assert isinstance(error, RestoreFailedClosed)
        assert "unable to find backup set" in str(error)
        assert "start" not in helper.calls
        service.start.assert_not_called()

    def test_a_cluster_that_will_not_start_fails_closed(self) -> None:
        error, _, service = self.attempt(
            FakeHelper(fail={"start": "did not start"}), World()
        )

        assert isinstance(error, RestoreFailedClosed)
        service.start.assert_not_called()

    def test_recovery_that_never_ends_fails_closed_and_stops_the_cluster(self) -> None:
        helper = FakeHelper()
        error, helper, _ = self.attempt(
            helper, World(promote_fails=TimeoutError("still in recovery after 600s"))
        )

        assert isinstance(error, RestoreFailedClosed)
        assert "still in recovery" in str(error)
        assert helper.calls[-1] == "stop"

    def test_a_tview_rebuild_failure_fails_closed(self) -> None:
        strategy, source, _ = build(FakeHelper())
        prepared = prepare(strategy, source)
        world = World(tviews=[])
        with (
            world,
            patch(
                "fraisier.dbops.tviews.rebuild_empty_tviews",
                side_effect=TviewError("refresh blew up"),
            ),
            pytest.raises(RestoreFailedClosed, match="refresh blew up"),
        ):
            source.restore(strategy, prepared)

    def test_a_failure_before_the_cluster_is_touched_is_an_ordinary_error(self) -> None:
        """Service stop fails: nothing was stopped or restored, so the service may restart."""
        strategy, source, service = build(FakeHelper())
        prepared = prepare(strategy, source)
        service.stop.side_effect = RuntimeError("systemctl failed")

        with pytest.raises(DatabaseError) as raised:
            source.restore(strategy, prepared)

        assert not isinstance(raised.value, RestoreFailedClosed)
        assert getattr(raised.value, "keep_service_stopped", False) is False

    def test_a_failed_cluster_stop_before_the_restore_is_an_ordinary_error(
        self,
    ) -> None:
        strategy, source, _ = build(FakeHelper(fail={"stop": "still running"}))
        prepared = prepare(strategy, source)

        with World(), pytest.raises(DatabaseError) as raised:
            source.restore(strategy, prepared)

        assert not isinstance(raised.value, RestoreFailedClosed)

    def test_the_failure_says_the_cluster_is_stopped_and_the_service_is_not_started(
        self,
    ) -> None:
        error, _, _ = self.attempt(FakeHelper(), World(archive_mode="on"))

        assert "cluster was stopped" in str(error)
        assert "service was not started" in str(error)
        assert isinstance(error, RestoreFailedClosed)
        assert error.keep_service_stopped is True


class TestTheErrorType:
    def test_a_closed_failure_is_a_database_error_that_keeps_the_service_down(
        self,
    ) -> None:
        error = RestoreFailedClosed("x")

        assert isinstance(error, DatabaseError)
        assert error.keep_service_stopped is True

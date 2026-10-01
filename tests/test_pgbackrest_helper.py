"""The pgBackRest root helper: what it runs, as whom, and what it refuses (#424).

The command runner is injected, so these assert the **exact argv** of every
action without a cluster — the Docker harness in ``tests/integration`` runs the
same operations against real PostgreSQL and pgBackRest.

What matters here is the shape of the trust: the request names an operation and
nothing else; stanza, repository, cluster and target are baked into the helper at
startup; the data directory comes from ``pg_lsclusters``, never from a request;
and pgBackRest runs as the cluster's owner, not as root.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from fraisier import pgbackrest_helper as helper
from fraisier.pgbackrest_protocol import Request

FIXTURES = Path(__file__).parent / "fixtures" / "pgbackrest"
ONLINE = (FIXTURES / "pg_lsclusters-staging-online.txt").read_text()
DOWN = (FIXTURES / "pg_lsclusters-staging-down.txt").read_text()
RESTORE_LOG = (FIXTURES / "restore-delta-detail.excerpt.log").read_text()
INFO = (FIXTURES / "info-full-and-incrs.json").read_text()
LABEL = "20261001-141744F_20261001-141820I"
DATADIR = "/var/lib/postgresql/18/staging"


def config(**overrides: Any) -> helper.HelperConfig:
    base = helper.HelperConfig(
        stanza="main",
        repo=1,
        version="18",
        name="staging",
        target="latest",
        timeout_seconds=3600,
    )
    return dataclasses.replace(base, **overrides)


class FakeRunner:
    """Records each command and answers from a script keyed by its first words."""

    def __init__(self, *answers: tuple[str, int, str, str]) -> None:
        self.answers = answers
        self.calls: list[dict] = []

    def __call__(self, argv: list[str], *, user: str | None, timeout: int):
        self.calls.append({"argv": argv, "user": user, "timeout": timeout})
        text = " ".join(argv)
        for match, returncode, stdout, stderr in self.answers:
            if match in text:
                return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        raise AssertionError(f"unscripted command: {text}")


def ops(runner: FakeRunner, **overrides: Any) -> helper.Operations:
    return helper.Operations(config(**overrides), run=runner)


class TestInfo:
    def test_it_reads_the_stanza_as_json_as_the_cluster_owner(self) -> None:
        runner = FakeRunner(
            ("pg_lsclusters", 0, ONLINE, ""), ("info --output=json", 0, INFO, "")
        )

        reply = ops(runner).handle(Request("info"))

        assert reply["ok"] is True
        assert reply["stdout"] == INFO
        info_call = runner.calls[-1]
        assert info_call["argv"] == [
            "/usr/bin/pgbackrest",
            "--stanza=main",
            "--repo=1",
            "info",
            "--output=json",
        ]
        assert info_call["user"] == "postgres"

    def test_a_failing_info_is_not_ok(self) -> None:
        runner = FakeRunner(
            ("pg_lsclusters", 0, ONLINE, ""), ("info", 56, "", "ERROR: [056]")
        )

        reply = ops(runner).handle(Request("info"))

        assert reply["ok"] is False
        assert reply["returncode"] == 56


class TestStatus:
    def test_it_reports_the_cluster_and_any_signal_files(self, tmp_path: Path) -> None:
        (tmp_path / "recovery.signal").write_text("")
        listing = f"18 staging 5433 down,recovery postgres {tmp_path} /var/log/x.log\n"
        runner = FakeRunner(("pg_lsclusters", 0, listing, ""))

        reply = ops(runner).handle(Request("status"))

        assert reply["ok"] is True
        assert reply["status"] == "down,recovery"
        assert reply["online"] is False
        assert reply["signals"] == ["recovery.signal"]

    def test_no_signals_when_the_cluster_is_clean(self, tmp_path: Path) -> None:
        listing = f"18 staging 5433 online postgres {tmp_path} /var/log/x.log\n"

        reply = ops(FakeRunner(("pg_lsclusters", 0, listing, ""))).handle(
            Request("status")
        )

        assert reply["signals"] == []
        assert reply["online"] is True

    def test_a_standby_signal_is_reported_too(self, tmp_path: Path) -> None:
        (tmp_path / "standby.signal").write_text("")
        listing = f"18 staging 5433 online postgres {tmp_path} /var/log/x.log\n"

        reply = ops(FakeRunner(("pg_lsclusters", 0, listing, ""))).handle(
            Request("status")
        )

        assert reply["signals"] == ["standby.signal"]


class TestStop:
    def test_an_online_cluster_is_stopped_fast_as_root_and_confirmed_down(self) -> None:
        runner = FakeRunner(
            ("pg_ctlcluster 18 staging stop", 0, "", ""),
        )
        listings = iter([ONLINE, DOWN])

        class Sequenced(FakeRunner):
            def __call__(self, argv, *, user, timeout):
                if "pg_lsclusters" in " ".join(argv):
                    self.calls.append({"argv": argv, "user": user, "timeout": timeout})
                    return subprocess.CompletedProcess(argv, 0, next(listings), "")
                return super().__call__(argv, user=user, timeout=timeout)

        runner = Sequenced(("pg_ctlcluster 18 staging stop", 0, "", ""))

        reply = ops(runner).handle(Request("stop"))

        assert reply["ok"] is True
        stop = next(c for c in runner.calls if "stop" in c["argv"])
        assert stop["argv"] == [
            "/usr/bin/pg_ctlcluster",
            "18",
            "staging",
            "stop",
            "-m",
            "fast",
        ]
        assert stop["user"] is None  # root: starting and stopping a cluster needs it

    def test_a_cluster_that_is_already_down_is_left_alone(self) -> None:
        runner = FakeRunner(("pg_lsclusters", 0, DOWN, ""))

        reply = ops(runner).handle(Request("stop"))

        assert reply["ok"] is True
        assert all("stop" not in c["argv"] for c in runner.calls)

    def test_a_stop_that_leaves_it_running_is_not_ok(self) -> None:
        runner = FakeRunner(
            ("pg_lsclusters", 0, ONLINE, ""), ("pg_ctlcluster", 0, "", "")
        )

        reply = ops(runner).handle(Request("stop"))

        assert reply["ok"] is False
        assert "still running" in reply["error"]


class TestRestore:
    def run(self, listing: str = DOWN, **overrides: Any) -> tuple[dict, FakeRunner]:
        runner = FakeRunner(
            ("pg_lsclusters", 0, listing, ""), ("restore", 0, RESTORE_LOG, "")
        )
        return ops(runner, **overrides).handle(Request("restore", LABEL)), runner

    def test_the_exact_command_for_latest(self) -> None:
        reply, runner = self.run()

        assert reply["ok"] is True
        restore = runner.calls[-1]
        assert restore["argv"] == [
            "/usr/bin/pgbackrest",
            "--stanza=main",
            "--repo=1",
            f"--pg1-path={DATADIR}",
            "--log-level-console=detail",
            "restore",
            "--delta",
            "--archive-mode=off",
            f"--set={LABEL}",
        ]
        assert restore["user"] == "postgres"
        assert restore["timeout"] == 3600

    def test_a_timestamp_target_adds_the_point_in_time_flags(self) -> None:
        """``--target-action`` is invalid without ``--type`` (measured), so it rides with it."""
        _, runner = self.run(target="2026-10-01 14:18:37+00")

        argv = runner.calls[-1]["argv"]
        assert argv[argv.index("--archive-mode=off") + 1 :] == [
            f"--set={LABEL}",
            "--type=time",
            "--target=2026-10-01 14:18:37+00",
            "--target-action=promote",
        ]

    def test_latest_adds_neither_a_type_nor_a_target_action(self) -> None:
        _, runner = self.run()

        argv = runner.calls[-1]["argv"]
        assert not any(a.startswith(("--type", "--target")) for a in argv)

    def test_the_data_directory_comes_from_pg_lsclusters(self) -> None:
        listing = "18 staging 5433 down postgres /srv/pg/staging18 /var/log/x.log\n"

        _, runner = self.run(listing)

        assert "--pg1-path=/srv/pg/staging18" in runner.calls[-1]["argv"]

    def test_it_reads_the_log_into_a_summary(self) -> None:
        reply, _ = self.run()

        assert reply["summary"]["label"] == "20261001-141744F_20261001-141746I"
        assert reply["summary"]["files_total"] == 1295
        assert reply["summary"]["files_rewritten"] == 7

    def test_a_running_cluster_is_never_restored_over(self) -> None:
        """pgBackRest refuses too; the helper does not even ask it to."""
        reply, runner = self.run(ONLINE)

        assert reply["ok"] is False
        assert "running" in reply["error"]
        assert all("restore" not in c["argv"] for c in runner.calls)

    def test_a_failed_restore_returns_the_tail_of_the_log(self) -> None:
        runner = FakeRunner(
            ("pg_lsclusters", 0, DOWN, ""),
            ("restore", 75, "line1\nP00 ERROR: [075]: unable to find backup set\n", ""),
        )

        reply = ops(runner).handle(Request("restore", LABEL))

        assert reply["ok"] is False
        assert reply["returncode"] == 75
        assert "unable to find backup set" in reply["tail"]

    def test_the_backup_set_is_one_argv_element_not_a_shell_string(self) -> None:
        _, runner = self.run()

        assert f"--set={LABEL}" in runner.calls[-1]["argv"]


class TestStart:
    def test_it_starts_the_cluster_as_root_and_confirms_it_is_online(self) -> None:
        listings = iter([DOWN, ONLINE])

        class Sequenced(FakeRunner):
            def __call__(self, argv, *, user, timeout):
                self.calls.append({"argv": argv, "user": user, "timeout": timeout})
                if "pg_lsclusters" in " ".join(argv):
                    return subprocess.CompletedProcess(argv, 0, next(listings), "")
                return subprocess.CompletedProcess(argv, 0, "", "")

        runner = Sequenced()

        reply = ops(runner).handle(Request("start"))

        assert reply["ok"] is True
        start = next(c for c in runner.calls if "start" in c["argv"])
        assert start["argv"] == ["/usr/bin/pg_ctlcluster", "18", "staging", "start"]
        assert start["user"] is None


class TestTheClusterMustExist:
    def test_an_unknown_cluster_is_an_error_not_a_guess(self) -> None:
        listing = (
            "18 prod 5432 online postgres /var/lib/postgresql/18/prod /var/log/x.log\n"
        )

        reply = ops(FakeRunner(("pg_lsclusters", 0, listing, ""))).handle(
            Request("stop")
        )

        assert reply["ok"] is False
        assert "18/staging" in reply["error"]

    def test_only_the_configured_cluster_is_ever_touched(self) -> None:
        runner = FakeRunner(
            ("pg_lsclusters", 0, ONLINE, ""), ("pg_ctlcluster", 0, "", "")
        )

        ops(runner).handle(Request("stop"))

        assert all("prod" not in " ".join(c["argv"]) for c in runner.calls)


class TestAtTheSocket:
    """One request per connection, validated before anything runs."""

    def exchange(self, payload: bytes, operations: helper.Operations, **kwargs) -> dict:
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        client.sendall(payload)
        helper._handle_connection(server, operations, **kwargs)
        with client.makefile("rb") as f:
            return json.loads(f.readline())

    def test_a_valid_request_is_executed_and_answered(self) -> None:
        runner = FakeRunner(("pg_lsclusters", 0, ONLINE, ""), ("info", 0, INFO, ""))

        reply = self.exchange(b'{"action": "info"}\n', ops(runner))

        assert reply["ok"] is True

    @pytest.mark.parametrize(
        "payload",
        [
            b'{"action": "info", "path": "/etc"}\n',
            b'{"action": "shell"}\n',
            b"garbage\n",
        ],
    )
    def test_a_rejected_request_runs_nothing(self, payload: bytes) -> None:
        runner = FakeRunner()

        reply = self.exchange(payload, ops(runner))

        assert reply["ok"] is False
        assert runner.calls == []

    def test_an_oversized_request_is_refused_before_it_is_parsed(self) -> None:
        runner = FakeRunner()

        reply = self.exchange(b'{"action": "' + b"x" * 10000 + b'"}\n', ops(runner))

        assert reply["ok"] is False
        assert runner.calls == []

    def test_a_peer_with_the_wrong_uid_is_rejected_before_anything_is_read(
        self,
    ) -> None:
        runner = FakeRunner()
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        client.sendall(b'{"action": "info"}\n')

        with patch(
            "fraisier.pgbackrest_helper.check_peer_creds",
            side_effect=PermissionError("uid=1234 rejected"),
        ):
            helper._serve_connection(server, ops(runner), expected_uid=1000)

        with client.makefile("rb") as f:
            reply = json.loads(f.readline())
        assert reply["ok"] is False
        assert "rejected" in reply["error"]
        assert runner.calls == []

    def test_a_missing_deploy_uid_refuses_everything(self) -> None:
        """Unlike the older helpers there is no transitional fallback: this one is new."""
        runner = FakeRunner()
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        client.sendall(b'{"action": "stop"}\n')

        helper._serve_connection(server, ops(runner), expected_uid=None)

        with client.makefile("rb") as f:
            assert json.loads(f.readline())["ok"] is False
        assert runner.calls == []


class TestStartup:
    def argv(self, **overrides: str) -> list[str]:
        values = {
            "--deploy-user": "root",
            "--stanza": "main",
            "--repo": "1",
            "--cluster": "18/staging",
            "--target": "latest",
            "--timeout": "3600",
            **overrides,
        }
        return [item for pair in values.items() for item in pair]

    def test_the_baked_arguments_become_the_config(self) -> None:
        uid, parsed = helper.parse_args(self.argv())

        assert parsed == config()
        assert uid == 0

    @pytest.mark.parametrize(
        "bad",
        [
            {"--stanza": "ma in"},
            {"--stanza": "../x"},
            {"--repo": "0"},
            {"--repo": "x"},
            {"--cluster": "staging"},
            {"--cluster": "18/a b"},
            {"--target": "yesterday"},
            {"--target": "2026-10-01"},
            {"--timeout": "5"},
        ],
    )
    def test_a_malformed_unit_is_refused_at_startup(self, bad: dict) -> None:
        """Defence in depth: the scaffold validated these, and so does the daemon."""
        with pytest.raises(SystemExit):
            helper.parse_args(self.argv(**bad))

    @pytest.mark.parametrize(
        "missing", ["--stanza", "--repo", "--cluster", "--deploy-user"]
    )
    def test_every_baked_argument_is_required(self, missing: str) -> None:
        argv = self.argv()
        index = argv.index(missing)
        del argv[index : index + 2]

        with pytest.raises(SystemExit):
            helper.parse_args(argv)

    def test_it_will_not_start_without_socket_activation(self, monkeypatch) -> None:
        monkeypatch.delenv("LISTEN_FDS", raising=False)
        monkeypatch.setattr("sys.argv", ["fraisier-pgbackrest-helper", *self.argv()])

        with pytest.raises(SystemExit):
            helper.main()

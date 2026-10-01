"""The client half of the pgBackRest helper, and choosing which backup to restore (#424).

``info`` is captured from pgBackRest 2.59.2: four backups (two fulls and two
incrementals), epoch seconds in ``timestamp``, sizes in bytes — and a stanza that
does not exist answers with exit 0 and ``status.code`` 1, which is why the verdict
is read from the JSON and not from the process.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fraisier.config.restore_source import PgBackRestSpec
from fraisier.dbops.pgbackrest import (
    BackupChoiceError,
    HelperClient,
    HelperUnavailableError,
    parse_info,
    select_backup,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pgbackrest"
INFO = (FIXTURES / "info-full-and-incrs.json").read_text()
UNKNOWN_STANZA = (FIXTURES / "info-unknown-stanza.json").read_text()

#: The captured backups' stop times, as UTC instants.
STOPS = {
    "20261001-141733F": datetime.fromtimestamp(1790864256, UTC),
    "20261001-141744F": datetime.fromtimestamp(1790864266, UTC),
    "20261001-141744F_20261001-141746I": datetime.fromtimestamp(1790864267, UTC),
    "20261001-141744F_20261001-141820I": datetime.fromtimestamp(1790864301, UTC),
}
NEWEST = "20261001-141744F_20261001-141820I"


def spec(target: str = "latest") -> PgBackRestSpec:
    return PgBackRestSpec(stanza="main", repo=1, cluster="18/staging", target=target)


class TestReadingInfo:
    def test_every_backup_is_read_with_its_label_stop_time_and_size(self) -> None:
        backups = parse_info(INFO, stanza="main")

        assert [b.label for b in backups] == list(STOPS)
        newest = backups[-1]
        assert newest.stop == STOPS[NEWEST]
        assert newest.size_bytes == 31779757
        assert newest.kind == "incr"

    def test_a_missing_stanza_is_an_error_even_though_info_exited_zero(self) -> None:
        with pytest.raises(BackupChoiceError, match="missing stanza path"):
            parse_info(UNKNOWN_STANZA, stanza="nope")

    def test_a_stanza_with_no_backups_is_an_error(self) -> None:
        empty = json.loads(INFO)
        empty[0]["backup"] = []

        with pytest.raises(BackupChoiceError, match="no backups"):
            parse_info(json.dumps(empty), stanza="main")

    def test_a_backup_that_recorded_an_error_is_never_offered(self) -> None:
        data = json.loads(INFO)
        data[0]["backup"][-1]["error"] = True

        backups = parse_info(json.dumps(data), stanza="main")

        assert NEWEST not in [b.label for b in backups]

    @pytest.mark.parametrize("raw", ["", "not json", "{}", "[]", '[{"name": "other"}]'])
    def test_anything_else_is_an_error_not_a_crash(self, raw: str) -> None:
        with pytest.raises(BackupChoiceError):
            parse_info(raw, stanza="main")


class TestChoosingABackup:
    def test_latest_is_the_backup_that_stopped_last(self) -> None:
        choice = select_backup(parse_info(INFO, stanza="main"), spec())

        assert choice.label == NEWEST

    def test_a_point_in_time_takes_the_latest_backup_that_stopped_before_it(
        self,
    ) -> None:
        """pgBackRest's own rule: a backup must be complete before the target."""
        target = "2026-10-01 14:17:46.5+00"  # 1790864266.5: after the 141744F full, before its first incr

        choice = select_backup(parse_info(INFO, stanza="main"), spec(target))

        assert choice.label == "20261001-141744F"

    def test_an_offset_is_honoured_not_read_as_utc(self) -> None:
        # The same instant written in UTC+02:00.
        choice = select_backup(
            parse_info(INFO, stanza="main"), spec("2026-10-01 16:17:46.5+02:00")
        )

        assert choice.label == "20261001-141744F"

    def test_a_target_before_every_backup_is_an_error_naming_the_earliest(self) -> None:
        with pytest.raises(BackupChoiceError, match="20261001-141733F"):
            select_backup(
                parse_info(INFO, stanza="main"), spec("2020-01-01 00:00:00+00")
            )

    def test_the_choice_reports_what_the_log_line_needs(self) -> None:
        choice = select_backup(parse_info(INFO, stanza="main"), spec())

        assert (choice.label, choice.kind, choice.size_bytes) == (
            NEWEST,
            "incr",
            31779757,
        )
        assert choice.stop == STOPS[NEWEST]


class FakeHelper:
    """A real AF_UNIX listener on a path, answering one scripted line per connection."""

    def __init__(self, path: Path, replies: list[bytes]) -> None:
        self.path = path
        self.replies = list(replies)
        self.requests: list[bytes] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while self.replies:
            conn, _ = self.server.accept()
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                self.requests.append(buf)
                conn.sendall(self.replies.pop(0))

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def helper_socket(tmp_path):
    started: list[FakeHelper] = []

    def start(*replies: dict) -> FakeHelper:
        fake = FakeHelper(
            tmp_path / "h.sock", [json.dumps(r).encode() + b"\n" for r in replies]
        )
        started.append(fake)
        return fake

    yield start
    for fake in started:
        fake.close()


class TestTheClient:
    def test_it_sends_exactly_one_action_per_connection(self, helper_socket) -> None:
        fake = helper_socket({"ok": True, "status": "down"})

        reply = HelperClient(str(fake.path)).stop()

        assert reply["ok"] is True
        assert json.loads(fake.requests[0]) == {"action": "stop"}

    def test_restore_names_the_set_and_nothing_else(self, helper_socket) -> None:
        fake = helper_socket({"ok": True, "summary": {}, "tail": ""})

        HelperClient(str(fake.path)).restore(NEWEST)

        assert json.loads(fake.requests[0]) == {"action": "restore", "set": NEWEST}

    def test_a_refusal_from_the_helper_is_an_error_with_its_message(
        self, helper_socket
    ) -> None:
        fake = helper_socket({"ok": False, "error": "cluster 18/staging is running"})

        with pytest.raises(HelperUnavailableError, match="18/staging is running"):
            HelperClient(str(fake.path)).restore(NEWEST)

    def test_no_socket_says_how_to_get_one(self, tmp_path) -> None:
        with pytest.raises(HelperUnavailableError, match="scaffold-install"):
            HelperClient(str(tmp_path / "absent.sock")).stop()

    def test_a_listener_that_never_answers_times_out_rather_than_hanging(
        self, tmp_path
    ) -> None:
        path = tmp_path / "silent.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        try:
            with pytest.raises(HelperUnavailableError, match="no answer"):
                HelperClient(str(path), timeout=0.2).stop()
        finally:
            server.close()

    def test_a_reply_that_is_not_json_is_an_error(self, tmp_path) -> None:
        path = tmp_path / "bad.sock"
        fake = FakeHelper(path, [b"garbage\n"])
        try:
            with pytest.raises(HelperUnavailableError, match="unreadable"):
                HelperClient(str(path)).stop()
        finally:
            fake.close()

    def test_restore_waits_as_long_as_the_spec_allows(self) -> None:
        client = HelperClient("/x", timeout=60, long_timeout=21600 + 60)

        assert client.timeout_for("restore") == 21660
        assert client.timeout_for("start") == 21660
        assert client.timeout_for("info") == 60

    def test_stop_waits_as_long_as_the_helper_does(self) -> None:
        """The helper allows a cluster 900s to stop; a client that gave up at 120s
        would report a failure while the helper went on stopping it."""
        client = HelperClient("/x", timeout=60, long_timeout=960)

        assert client.timeout_for("stop") == 960

    def test_a_reply_that_never_ends_is_refused_at_a_size_cap(self, tmp_path) -> None:
        path = tmp_path / "huge.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)

        def flood() -> None:
            conn, _ = server.accept()
            with conn, contextlib.suppress(OSError):
                conn.recv(100)
                for _ in range(64):
                    conn.sendall(b"x" * (1024 * 1024))

        threading.Thread(target=flood, daemon=True).start()
        try:
            with pytest.raises(HelperUnavailableError, match="too large"):
                HelperClient(str(path), timeout=5).info()
        finally:
            server.close()

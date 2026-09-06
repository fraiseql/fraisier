"""A root helper retires itself when the fraisier under it is replaced (#391).

`maybe_self_upgrade` installs a newer fraisier into the deploy user's venv and
restarts one unit: the webhook. All four root helpers run from that same venv,
are `Type=simple`, and none of their sockets set `Accept=` — so systemd
defaults to `Accept=no`: one process gets the listening fd and serves every
connection from a `while True: accept()` loop. They kept running the old code
until someone ran `scaffold-install`.

The helpers now notice and exit, and socket activation brings the next
connection up on the new code.
"""

from __future__ import annotations

import socket
import threading
import time
from unittest.mock import patch

import pytest

from fraisier.helper_version import VersionWatch, serve_until_stale


class TestVersionWatch:
    def test_an_unchanged_version_is_not_stale(self):
        with patch("fraisier.helper_version.installed_version", return_value="1.2.3"):
            watch = VersionWatch()
            assert watch.started_with == "1.2.3"
            assert watch.is_stale() is False

    def test_a_moved_version_is_stale(self):
        with patch("fraisier.helper_version.installed_version", return_value="1.2.3"):
            watch = VersionWatch()
        with patch("fraisier.helper_version.installed_version", return_value="1.3.0"):
            assert watch.is_stale() is True

    def test_a_downgrade_is_stale_too(self):
        """Any change means the code on disk is not the code in this process."""
        with patch("fraisier.helper_version.installed_version", return_value="1.3.0"):
            watch = VersionWatch()
        with patch("fraisier.helper_version.installed_version", return_value="1.2.3"):
            assert watch.is_stale() is True

    def test_an_unreadable_version_now_is_not_stale(self):
        """A venv mid-upgrade must not end a root daemon."""
        with patch("fraisier.helper_version.installed_version", return_value="1.2.3"):
            watch = VersionWatch()
        with patch("fraisier.helper_version.installed_version", return_value=None):
            assert watch.is_stale() is False

    def test_an_unreadable_version_at_startup_never_goes_stale(self):
        """With no baseline there is nothing to compare against; stay up."""
        with patch("fraisier.helper_version.installed_version", return_value=None):
            watch = VersionWatch()
        with patch("fraisier.helper_version.installed_version", return_value="9.9.9"):
            assert watch.is_stale() is False

    def test_installed_version_reads_this_package(self):
        from fraisier import __version__
        from fraisier.helper_version import installed_version

        assert installed_version() == __version__

    def test_installed_version_survives_a_broken_dist_info(self):
        import importlib.metadata

        from fraisier.helper_version import installed_version

        with patch.object(
            importlib.metadata,
            "version",
            side_effect=importlib.metadata.PackageNotFoundError("fraisier"),
        ):
            assert installed_version() is None


def _connect_and_send(path: str, payload: bytes = b"ping\n") -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(path)
        sock.sendall(payload)


@pytest.fixture
def server_pair(tmp_path):
    """A listening AF_UNIX socket plus its path, as systemd would hand over."""
    path = tmp_path / "helper.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(8)
    try:
        yield sock, str(path)
    finally:
        sock.close()


class TestServeUntilStale:
    def test_it_serves_connections(self, server_pair):
        sock, path = server_pair
        served: list[bytes] = []
        stale = threading.Event()

        def handle(conn):
            with conn:
                served.append(conn.recv(64))
            if len(served) >= 2:
                stale.set()

        thread = threading.Thread(
            target=serve_until_stale,
            args=(sock, handle),
            kwargs={"is_stale": stale.is_set, "poll_interval": 0.05},
            daemon=True,
        )
        thread.start()
        _connect_and_send(path, b"one\n")
        _connect_and_send(path, b"two\n")
        thread.join(timeout=5)

        assert not thread.is_alive(), "the loop did not return"
        assert served == [b"one\n", b"two\n"]

    def test_it_returns_when_stale_with_no_traffic_at_all(self, server_pair):
        """The loop blocks in accept(); a helper that only checked after
        serving would keep old code until something called it — and the caller
        that arrived first would be served by the stale process."""
        sock, _path = server_pair
        stale = threading.Event()
        stale.set()

        started = time.monotonic()
        thread = threading.Thread(
            target=serve_until_stale,
            args=(sock, lambda conn: conn.close()),
            kwargs={"is_stale": stale.is_set, "poll_interval": 0.05},
            daemon=True,
        )
        thread.start()
        thread.join(timeout=5)

        assert not thread.is_alive(), "the loop never noticed without traffic"
        assert time.monotonic() - started < 5

    def test_an_in_flight_request_is_finished_first(self, server_pair):
        """Exiting between accept() and the reply would drop a request."""
        sock, path = server_pair
        finished: list[str] = []
        stale = threading.Event()

        def handle(conn):
            with conn:
                conn.recv(64)
                # The upgrade lands while this request is being served.
                stale.set()
                time.sleep(0.1)
                conn.sendall(b"done\n")
            finished.append("yes")

        thread = threading.Thread(
            target=serve_until_stale,
            args=(sock, handle),
            kwargs={"is_stale": stale.is_set, "poll_interval": 0.05},
            daemon=True,
        )
        thread.start()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(path)
            client.sendall(b"go\n")
            assert client.recv(64) == b"done\n"
        thread.join(timeout=5)

        assert finished == ["yes"]
        assert not thread.is_alive()

    def test_a_handler_crash_does_not_end_the_loop(self, server_pair):
        """Unchanged contract: a handler crash must not bring down a
        systemd-supervised socket server."""
        sock, path = server_pair
        calls: list[int] = []
        stale = threading.Event()

        def handle(conn):
            conn.close()
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            stale.set()

        thread = threading.Thread(
            target=serve_until_stale,
            args=(sock, handle),
            kwargs={"is_stale": stale.is_set, "poll_interval": 0.05},
            daemon=True,
        )
        thread.start()
        _connect_and_send(path)
        _connect_and_send(path)
        thread.join(timeout=5)

        assert calls == [1, 1]
        assert not thread.is_alive()

    def test_it_never_exits_while_fresh(self, server_pair):
        sock, path = server_pair
        thread = threading.Thread(
            target=serve_until_stale,
            args=(sock, lambda conn: conn.close()),
            kwargs={"is_stale": lambda: False, "poll_interval": 0.02},
            daemon=True,
        )
        thread.start()
        _connect_and_send(path)
        thread.join(timeout=0.4)

        assert thread.is_alive(), "a fresh helper exited"
        sock.close()  # unblock the loop so the thread can finish


class TestEveryHelperRetiresItself:
    """A fifth helper must not be able to forget.

    Each of these runs from the deploy user's venv behind an `Accept=no`
    socket, so each keeps old code after an upgrade until it ends itself.
    """

    HELPERS = (
        "systemctl_helper",
        "install_helper",
        "scaffold_install_helper",
        "unit_installer_helper",
    )

    @pytest.mark.parametrize("module_name", HELPERS)
    def test_the_helper_uses_the_shared_loop(self, module_name):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent / "fraisier" / f"{module_name}.py"
        ).read_text()
        assert "serve_until_stale" in source, (
            f"{module_name} has its own accept loop; an upgrade would leave it "
            "serving the old code forever (#391)"
        )

    @pytest.mark.parametrize("module_name", HELPERS)
    def test_the_helper_has_no_bare_accept_loop_left(self, module_name):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent / "fraisier" / f"{module_name}.py"
        ).read_text()
        assert "server_sock.accept()" not in source, (
            f"{module_name} still accepts directly; the staleness check is "
            "bypassed (#391)"
        )

    @pytest.mark.parametrize("module_name", HELPERS)
    def test_the_helper_main_returns_when_stale(self, module_name, tmp_path):
        """`main()` must come back, not spin, once the version has moved."""
        import importlib

        module = importlib.import_module(f"fraisier.{module_name}")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(tmp_path / f"{module_name}.sock"))
        sock.listen(8)

        done = threading.Event()
        failure: list[BaseException] = []

        def _run():
            try:
                module.serve_until_stale(
                    sock,
                    lambda conn: conn.close(),
                    is_stale=lambda: True,
                    poll_interval=0.05,
                )
            except BaseException as exc:  # reported below, not swallowed
                failure.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        returned = done.wait(timeout=5)
        sock.close()

        assert not failure, f"{module_name}.serve_until_stale raised {failure[0]!r}"
        assert returned, f"{module_name} did not return when stale"

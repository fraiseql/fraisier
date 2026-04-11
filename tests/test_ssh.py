"""Tests for the fraisier.ssh invocation abstraction.

The ``fraisier.ssh`` module centralises every subprocess-based SSH call in
the codebase so that the defensive flag set learned the hard way in
``cli/logs.py`` (see `.phases/2026-04-10-ssh-io-contract/inventory.md`) is
applied by construction to every pattern:

- ``short_cmd``  — run a remote command and capture output
- ``long_stream`` — tail a remote process, caller owns the Popen
- ``data_pipe``  — feed a local stream (e.g. tar) into SSH stdin

Phase 2 only adds the module; Phase 3 migrates call sites onto it.
"""

from __future__ import annotations

import subprocess
from itertools import pairwise
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from fraisier.ssh import (
    SshTarget,
    cmd_with_input,
    data_pipe,
    long_stream,
    scp_options,
    short_cmd,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Cycle 1 — SshTarget + _options()
# ---------------------------------------------------------------------------


class TestSshTargetFromConfig:
    """``SshTarget.from_config`` accepts the same dict shape that every
    existing call site already consumes (``logs.py``, ``runners.py``,
    ``validation.py``, ``bare_metal.py``)."""

    def test_defaults_from_minimal_config(self):
        target = SshTarget.from_config({"host": "deploy.example.com"})
        assert target.host == "deploy.example.com"
        assert target.user == "root"
        assert target.port == 22
        assert target.key_path is None
        assert target.strict_host_key is True
        assert target.connect_timeout == 30
        assert target.address_family is None

    def test_all_fields_populated(self):
        target = SshTarget.from_config(
            {
                "host": "h",
                "user": "u",
                "port": 2222,
                "key_path": "/etc/keys/id_ed25519",
                "strict_host_key": False,
                "connect_timeout": 5,
                "address_family": "inet",
            }
        )
        assert target.host == "h"
        assert target.user == "u"
        assert target.port == 2222
        assert target.key_path == "/etc/keys/id_ed25519"
        assert target.strict_host_key is False
        assert target.connect_timeout == 5
        assert target.address_family == "inet"

    def test_instance_is_frozen(self):
        target = SshTarget.from_config({"host": "h"})
        with pytest.raises((AttributeError, TypeError)):
            target.host = "other"  # type: ignore[misc]

    def test_missing_host_raises(self):
        with pytest.raises((KeyError, TypeError, ValueError)):
            SshTarget.from_config({})


class TestSshTargetOptions:
    """``_options()`` returns the shared ``-o ...`` block (and ``-i`` when a
    key is configured). It must be usable by both ``ssh`` and ``scp``; the
    only difference between the two is ``-p`` vs ``-P`` for the port, which
    is handled by the callers, not by ``_options()``.
    """

    def _options(self, **overrides) -> list[str]:
        cfg = {"host": "h", **overrides}
        return SshTarget.from_config(cfg)._options()

    def test_defaults_include_the_full_defensive_set(self):
        opts = self._options()
        # Paired -o / value entries — assert contiguous pairs, not just
        # "in opts", so we catch accidental splitting.
        assert ("-o", "StrictHostKeyChecking=accept-new") in _pairs(opts)
        assert ("-o", "BatchMode=yes") in _pairs(opts)
        assert ("-o", "ConnectTimeout=30") in _pairs(opts)

    def test_strict_host_key_false_flips_to_no(self):
        opts = self._options(strict_host_key=False)
        assert ("-o", "StrictHostKeyChecking=no") in _pairs(opts)
        assert ("-o", "StrictHostKeyChecking=accept-new") not in _pairs(opts)

    def test_custom_connect_timeout(self):
        opts = self._options(connect_timeout=5)
        assert ("-o", "ConnectTimeout=5") in _pairs(opts)

    def test_address_family_only_added_when_set(self):
        opts = self._options()
        assert not any(
            p[0] == "-o" and p[1].startswith("AddressFamily=") for p in _pairs(opts)
        )
        opts = self._options(address_family="inet")
        assert ("-o", "AddressFamily=inet") in _pairs(opts)

    def test_key_path_adds_dash_i(self):
        opts = self._options()
        assert "-i" not in opts
        opts = self._options(key_path="/tmp/k")
        assert "-i" in opts
        assert opts[opts.index("-i") + 1] == "/tmp/k"

    def test_options_do_not_include_port_or_dash_n(self):
        """`_options` is shared between ssh and scp; port (-p/-P) and -n
        are set by the specific entry point, never in the shared block."""
        opts = self._options(port=2222)
        assert "-p" not in opts
        assert "-P" not in opts
        assert "-n" not in opts
        assert "2222" not in opts


def _pairs(seq: list[str]) -> list[tuple[str, str]]:
    """Return consecutive pairs from a flat list — used to assert that a
    ``-o`` flag and its argument sit next to each other, not just both
    present somewhere in the list."""
    return list(pairwise(seq))


# ---------------------------------------------------------------------------
# Cycle 2 — short_cmd happy path
# ---------------------------------------------------------------------------


class TestShortCmd:
    """``short_cmd`` is the default pattern: run a remote command, capture
    output, return a ``CompletedProcess``. Flags correspond to the
    ``short-cmd`` row in ``inventory.md``: ``-n`` is required here (parent
    never writes to stdin), along with the full defensive ``-o`` block.
    """

    _target = SshTarget.from_config(
        {"host": "deploy.example.com", "user": "fraisier", "port": 2222}
    )

    def _fake_completed(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="ok\n", stderr=""
        )

    def test_builds_ssh_argv_with_dash_n_and_port(self):
        with patch("fraisier.ssh.subprocess.run") as mock_run:
            mock_run.return_value = self._fake_completed([])
            short_cmd(self._target, ["systemctl", "is-active", "api.service"])

        argv = mock_run.call_args.args[0]
        # First token is the binary.
        assert argv[0] == "ssh"
        # -n must be present for the short-cmd pattern — see LB-2 and
        # commit da5c119. Must come before the host.
        assert "-n" in argv
        host_idx = argv.index("fraisier@deploy.example.com")
        assert argv.index("-n") < host_idx
        # Port is set via -p (ssh), not -P (scp), and sits next to its value.
        p_idx = argv.index("-p")
        assert argv[p_idx + 1] == "2222"
        # Remote argv is appended at the very end, joined shell-safe.
        assert argv[-1] == "systemctl is-active api.service"

    def test_captures_stdout_and_returns_completed_process(self):
        with patch("fraisier.ssh.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="active\n", stderr=""
            )
            result = short_cmd(self._target, ["systemctl", "is-active", "api"])

        assert result.returncode == 0
        assert result.stdout == "active\n"
        # subprocess.run must be called with capture_output=True and
        # text=True so callers get str back, not bytes.
        kwargs = mock_run.call_args.kwargs
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 60

    def test_check_false_is_forwarded(self):
        with patch("fraisier.ssh.subprocess.run") as mock_run:
            mock_run.return_value = self._fake_completed([])
            short_cmd(self._target, ["false"], check=False)
        assert mock_run.call_args.kwargs["check"] is False

    def test_does_not_allocate_stdin(self):
        """Guard against future edits that accidentally wire a stdin=
        kwarg into short_cmd — that would defeat -n and re-introduce the
        LB-2 hang (see ``latent-bugs.md``)."""
        with patch("fraisier.ssh.subprocess.run") as mock_run:
            mock_run.return_value = self._fake_completed([])
            short_cmd(self._target, ["true"])
        kwargs = mock_run.call_args.kwargs
        assert "stdin" not in kwargs or kwargs["stdin"] is None

    def test_remote_argv_is_shell_joined_not_list(self):
        """ssh takes a single remote-command string, not an argv list.
        short_cmd must shell-quote so e.g. ``["echo", "a b"]`` survives."""
        with patch("fraisier.ssh.subprocess.run") as mock_run:
            mock_run.return_value = self._fake_completed([])
            short_cmd(self._target, ["echo", "a b", "c;d"])
        argv = mock_run.call_args.args[0]
        assert argv[-1] == "echo 'a b' 'c;d'"


# ---------------------------------------------------------------------------
# Cycle 3 — short_cmd timeout
# ---------------------------------------------------------------------------


class TestShortCmdTimeout:
    """The 60s default is deliberate (matches logs.py's tolerance for
    journalctl-shaped commands); callers can override it, and
    ``TimeoutExpired`` must propagate so the caller can react."""

    _target = SshTarget.from_config({"host": "h"})

    def test_custom_timeout_is_passed_through(self):
        with patch("fraisier.ssh.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            short_cmd(self._target, ["true"], timeout=7)
        assert mock_run.call_args.kwargs["timeout"] == 7

    def test_timeout_expired_propagates(self):
        with patch("fraisier.ssh.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=1)
            with pytest.raises(subprocess.TimeoutExpired):
                short_cmd(self._target, ["sleep", "60"], timeout=1)


# ---------------------------------------------------------------------------
# Cycle 4 — long_stream (Popen + SIGINT forwarding)
# ---------------------------------------------------------------------------


class TestLongStream:
    """``long_stream`` is the pattern used by ``fraisier logs …``: spawn a
    Popen, inherit stdout/stderr to the terminal, and let the caller
    wait + forward SIGINT (Ctrl-C) via ``terminate()``.

    The critical defensive flags (baked in by fix commits 8fc8fec,
    08265c9, da5c119 — see inventory ``Per-flag rationale`` table):

    - ``subprocess.Popen`` (not ``os.execvp``), so the parent stays
      alive to signal the child.
    - ``stdin=DEVNULL``, so SSH doesn't wait forever on an inherited
      never-closing pipe.
    - ``-n`` on the ssh argv itself, so SSH doesn't allocate a stdin
      channel even if something slips past ``DEVNULL``.
    - ``stdout``/``stderr`` NOT set, so the TTY is inherited and
      colour/size/interactive behaviour still work.
    """

    _target = SshTarget.from_config({"host": "h", "user": "u"})

    def test_returns_popen_with_devnull_stdin(self):
        with patch("fraisier.ssh.subprocess.Popen") as mock_popen:
            fake = object()
            mock_popen.return_value = fake
            result = long_stream(self._target, ["journalctl", "-f", "-u", "x"])

        assert result is fake
        kwargs = mock_popen.call_args.kwargs
        assert kwargs["stdin"] is subprocess.DEVNULL
        # stdout/stderr must inherit — do NOT set them to PIPE.
        assert "stdout" not in kwargs or kwargs["stdout"] is None
        assert "stderr" not in kwargs or kwargs["stderr"] is None

    def test_argv_includes_dash_n_and_shell_joined_remote(self):
        with patch("fraisier.ssh.subprocess.Popen") as mock_popen:
            mock_popen.return_value = object()
            long_stream(self._target, ["journalctl", "--no-pager", "-u", "a b"])

        argv = mock_popen.call_args.args[0]
        assert argv[0] == "ssh"
        assert "-n" in argv
        assert argv[-1] == "journalctl --no-pager -u 'a b'"

    def test_sigint_forwarding_terminates_child(self):
        """Integration test: spawn a real long-running child via
        :func:`long_stream`-style primitives and prove that
        ``terminate()`` (what the caller calls on KeyboardInterrupt)
        actually shuts it down. We can't hit a real SSH server in the
        unit suite, so we patch ``subprocess.Popen`` to swap ``ssh``
        for ``sleep`` — this exercises the *same* caller-owned lifecycle
        the real code will use.
        """
        real_popen = subprocess.Popen

        def fake_popen(_argv, **kwargs):
            # Ignore the ssh argv; run a harmless long sleep with the
            # same stdin/stdout/stderr discipline the real call would.
            return real_popen(["sleep", "30"], **kwargs)

        with patch("fraisier.ssh.subprocess.Popen", side_effect=fake_popen):
            proc = long_stream(self._target, ["journalctl", "-f"])

        try:
            # The caller's SIGINT handler: terminate + wait.
            proc.terminate()
            rc = proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)

        # SIGTERM → exit code is -SIGTERM under POSIX.
        assert rc != 0


# ---------------------------------------------------------------------------
# Cycle 5 — data_pipe (real tar round-trip)
# ---------------------------------------------------------------------------


class TestDataPipe:
    """``data_pipe`` is the ``upload_tree`` shape: the parent opens a
    subprocess (``tar czf -``) and hands its stdout to SSH as stdin.
    Unlike ``short_cmd``/``long_stream``, this pattern MUST NOT pass
    ``-n`` — SSH has to read stdin for the tar stream.

    The rest of the defensive flag set (``BatchMode``, ``ConnectTimeout``,
    ``StrictHostKeyChecking``, ``AddressFamily``) still applies. LB-5 in
    ``latent-bugs.md`` is closed by this test: the upload path now has
    the same IPv6-fallback protection as the short-cmd path.
    """

    _target = SshTarget.from_config({"host": "h", "user": "u"})

    def test_no_dash_n_but_keeps_connect_timeout_et_al(self):
        captured: dict[str, object] = {}

        def capture(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=b"", stderr=b""
            )

        with patch("fraisier.ssh.subprocess.run", side_effect=capture):
            data_pipe(self._target, ["tar", "xzf", "-", "-C", "/dest"], stdin=0)

        argv = captured["argv"]
        assert isinstance(argv, list)
        # -n MUST NOT be present on the data-pipe pattern.
        assert "-n" not in argv
        # But every other defensive flag is still present.
        assert ("-o", "BatchMode=yes") in _pairs(argv)
        assert ("-o", "ConnectTimeout=30") in _pairs(argv)
        assert ("-o", "StrictHostKeyChecking=accept-new") in _pairs(argv)

    def test_stdin_is_forwarded_and_not_captured_text(self):
        with patch("fraisier.ssh.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )
            data_pipe(self._target, ["tar", "xzf", "-"], stdin=42, timeout=10)

        kwargs = mock_run.call_args.kwargs
        assert kwargs["stdin"] == 42
        # Output is bytes (capture_output=True, no text=True).
        assert kwargs["capture_output"] is True
        assert kwargs.get("text") in (None, False)
        assert kwargs["timeout"] == 10

    def test_real_tar_round_trip(self, tmp_path: Path):
        """End-to-end: tar a local tree, pipe through ``data_pipe``
        (with ssh swapped for a local ``sh -c`` stand-in), and verify
        the tree materialises on the destination side.
        """
        src = tmp_path / "src"
        src.mkdir()
        (src / "hello.txt").write_text("hi\n")
        (src / "sub").mkdir()
        (src / "sub" / "nested.txt").write_text("nested\n")

        dest = tmp_path / "dest"
        dest.mkdir()

        real_run = subprocess.run

        def run_locally(argv, **kwargs):
            # argv ends with the shell-joined remote command; run it
            # under a local shell instead of shelling out via ssh.
            # This validates that stdin is actually threaded through.
            assert "-n" not in argv, "data_pipe must not pass -n"
            remote = argv[-1]
            return real_run(["sh", "-c", remote], **kwargs)

        tar = subprocess.Popen(
            ["tar", "czf", "-", "-C", str(src), "."],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            with patch("fraisier.ssh.subprocess.run", side_effect=run_locally):
                result = data_pipe(
                    self._target,
                    ["tar", "xzf", "-", "-C", str(dest)],
                    stdin=tar.stdout,
                )
        finally:
            if tar.stdout:
                tar.stdout.close()
            tar.wait(timeout=5)

        assert tar.returncode == 0
        assert result.returncode == 0
        assert (dest / "hello.txt").read_text() == "hi\n"
        assert (dest / "sub" / "nested.txt").read_text() == "nested\n"


# ---------------------------------------------------------------------------
# cmd_with_input — short_cmd shape with caller-supplied stdin payload
# ---------------------------------------------------------------------------


class TestCmdWithInput:
    """``cmd_with_input`` is the small fourth pattern: same defensive flag
    set as ``short_cmd`` (BatchMode, ConnectTimeout, etc.) but the caller
    feeds a small text payload on stdin via ``subprocess.run(input=...)``.
    The motivating use case is ``sudo -S`` — SSHRunner needs to pipe a
    sudo password to the remote sudo while still capturing stdout/stderr
    as text.

    Crucially, this pattern MUST omit ``-n``: ``-n`` would close ssh's
    own stdin and the remote ``sudo -S`` would never see the password.
    """

    _target = SshTarget.from_config({"host": "h", "user": "u"})

    def test_argv_omits_dash_n_but_keeps_full_defensive_set(self):
        with patch("fraisier.ssh.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            cmd_with_input(self._target, ["sudo", "-S", "true"], input="pw\n")
        argv = mock_run.call_args.args[0]
        assert argv[0] == "ssh"
        assert "-n" not in argv
        assert ("-o", "BatchMode=yes") in _pairs(argv)
        assert ("-o", "ConnectTimeout=30") in _pairs(argv)
        assert ("-o", "StrictHostKeyChecking=accept-new") in _pairs(argv)

    def test_input_is_passed_through_in_text_mode(self):
        with patch("fraisier.ssh.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="active\n", stderr=""
            )
            result = cmd_with_input(
                self._target, ["sudo", "-S", "systemctl", "is-active", "x"],
                input="secret\n",
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs["input"] == "secret\n"
        assert kwargs["text"] is True
        assert kwargs["capture_output"] is True
        # input= and stdin= are mutually exclusive in subprocess.run.
        assert "stdin" not in kwargs or kwargs["stdin"] is None
        assert result.stdout == "active\n"

    def test_remote_argv_is_shell_joined(self):
        with patch("fraisier.ssh.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            cmd_with_input(self._target, ["echo", "a b"], input="x")
        assert mock_run.call_args.args[0][-1] == "echo 'a b'"

    def test_check_and_timeout_forwarded(self):
        with patch("fraisier.ssh.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            cmd_with_input(
                self._target, ["true"], input="x", timeout=12, check=False,
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs["timeout"] == 12
        assert kwargs["check"] is False


# ---------------------------------------------------------------------------
# scp_options — shared flag set for the scp upload path
# ---------------------------------------------------------------------------


class TestScpOptions:
    """``scp_options`` returns the shared defensive flag set with ``-P``
    (capital, scp's port flag) instead of ``-p``. SSHRunner.upload uses
    it so the scp invocation gets the same defensive flags as the ssh
    paths — closing LB-7 from ``latent-bugs.md``.

    Note: ``scp_options`` does NOT include ``"scp"`` itself or src/dest;
    that's the caller's responsibility.
    """

    def _opts(self, **overrides) -> list[str]:
        target = SshTarget.from_config({"host": "h", **overrides})
        return scp_options(target)

    def test_includes_full_defensive_flag_set(self):
        opts = self._opts()
        assert ("-o", "BatchMode=yes") in _pairs(opts)
        assert ("-o", "ConnectTimeout=30") in _pairs(opts)
        assert ("-o", "StrictHostKeyChecking=accept-new") in _pairs(opts)

    def test_uses_capital_P_for_port_not_lowercase(self):
        opts = self._opts(port=2222)
        assert "-P" in opts
        assert opts[opts.index("-P") + 1] == "2222"
        assert "-p" not in opts

    def test_omits_dash_n(self):
        """``-n`` is an ssh-only flag (rejected by scp); never include it."""
        assert "-n" not in self._opts()

    def test_address_family_threaded_through(self):
        opts = self._opts(address_family="inet")
        assert ("-o", "AddressFamily=inet") in _pairs(opts)

    def test_key_path_threaded_through(self):
        opts = self._opts(key_path="/etc/keys/id_ed25519")
        assert "-i" in opts
        assert opts[opts.index("-i") + 1] == "/etc/keys/id_ed25519"

    def test_does_not_include_scp_binary(self):
        """Caller prepends 'scp' itself; scp_options is the flag block only."""
        assert "scp" not in self._opts()


# ---------------------------------------------------------------------------
# Cycle 6 — default flag-set review (cross-cutting)
# ---------------------------------------------------------------------------


class TestDefaultDefensiveFlags:
    """Cross-cutting guarantee: every public entry point includes the
    core three-flag defensive set by default — ``BatchMode=yes``,
    ``StrictHostKeyChecking=accept-new``, ``ConnectTimeout=30``.

    These three together close LB-1, LB-5, LB-7 and the prompt-hang
    failure mode. A future refactor that removes the shared
    ``_options()`` helper must still satisfy this test.
    """

    _target = SshTarget.from_config({"host": "h"})
    _expected = (
        ("-o", "BatchMode=yes"),
        ("-o", "StrictHostKeyChecking=accept-new"),
        ("-o", "ConnectTimeout=30"),
    )

    def _capture_argv(self, invoke) -> list[str]:
        captured: list[list[str]] = []

        def capture(argv, **_kwargs):
            captured.append(argv)
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout="" if _kwargs.get("text") else b"",
                stderr="" if _kwargs.get("text") else b"",
            )

        with (
            patch("fraisier.ssh.subprocess.run", side_effect=capture),
            patch("fraisier.ssh.subprocess.Popen", side_effect=capture),
        ):
            invoke()
        return captured[0]

    def test_short_cmd_has_all_three(self):
        argv = self._capture_argv(lambda: short_cmd(self._target, ["true"]))
        for pair in self._expected:
            assert pair in _pairs(argv)

    def test_long_stream_has_all_three(self):
        argv = self._capture_argv(
            lambda: long_stream(self._target, ["journalctl", "-f"])
        )
        for pair in self._expected:
            assert pair in _pairs(argv)

    def test_data_pipe_has_all_three(self):
        argv = self._capture_argv(
            lambda: data_pipe(self._target, ["tar", "xzf", "-"], stdin=0)
        )
        for pair in self._expected:
            assert pair in _pairs(argv)

    def test_only_data_pipe_omits_dash_n(self):
        """-n is the one flag that legitimately differs between
        patterns: short_cmd/long_stream require it, data_pipe forbids it."""
        short = self._capture_argv(lambda: short_cmd(self._target, ["true"]))
        stream = self._capture_argv(
            lambda: long_stream(self._target, ["journalctl", "-f"])
        )
        pipe = self._capture_argv(
            lambda: data_pipe(self._target, ["tar", "xzf", "-"], stdin=0)
        )
        assert "-n" in short
        assert "-n" in stream
        assert "-n" not in pipe

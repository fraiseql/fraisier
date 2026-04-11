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
from unittest.mock import patch

import pytest

from fraisier.ssh import SshTarget, short_cmd

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

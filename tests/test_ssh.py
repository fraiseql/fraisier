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

from itertools import pairwise

import pytest

from fraisier.ssh import SshTarget

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

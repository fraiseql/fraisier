"""A self-upgrade the running interpreter cannot satisfy touches nothing (#435).

`uv tool install --force` removes the tool before it verifies. confiture 1.30
and the fraisier release that follows it declare ``requires-python >=3.14``,
and since v0.84.2 the upgrade is pinned to the interpreter already running, so
a host on 3.13 would hand uv an install that can only fail, after the removal.

The worker therefore asks uv to resolve the target against the running
interpreter first (``uv pip install --dry-run``, which changes nothing and reads
the same index configuration), and stops there when it cannot.
"""

from __future__ import annotations

import os
import stat
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from fraisier.self_upgrade_record import read_self_upgrade_failure
from fraisier.webhook_self_upgrade import (
    _build_preflight_cmd,
    _run_upgrade,
)

SERVICE = "fraisier-api-webhook.service"


@pytest.fixture
def lock_dir(tmp_path):
    d = tmp_path / "run-fraisier"
    d.mkdir()
    return d


@pytest.fixture
def unit_dir(tmp_path, monkeypatch):
    d = tmp_path / "systemd"
    d.mkdir()
    monkeypatch.setattr("fraisier.webhook_self_upgrade._UNIT_DIR", d)
    return d


def _fake_uv(tmp_path: Path, monkeypatch, *, resolves: bool):
    """A `uv` that records every call; `tool install` removes the entrypoint."""
    bin_dir = tmp_path / "tools" / "bin"
    bin_dir.mkdir(parents=True)
    entry = bin_dir / "fraisier-webhook"
    entry.write_text("#!/bin/sh\necho ok\n")
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    calls = tmp_path / "uv-calls.log"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    uv = shim_dir / "uv"
    refuse = (
        "echo 'No solution found: fraisier requires Python >=3.14' >&2\nexit 1\n"
        if not resolves
        else "exit 0\n"
    )
    uv.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{calls}"\n'
        'if [ "$1 $2" = "pip install" ]; then\n'
        f"{refuse}"
        "fi\n"
        f"rm -rf '{bin_dir}'\n"
        "exit 0\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    return entry, calls


def _upgrade(lock_dir: Path):
    with patch("fraisier.webhook_self_upgrade._send_restart") as send_restart:
        rc = _run_upgrade(
            "9.0.0",
            SERVICE,
            "/run/fraisier/systemctl.sock",
            lock_dir=lock_dir,
            drain_settle_s=0,
        )
    return rc, send_restart


class TestThePreflightCommand:
    def test_resolves_the_target_against_the_running_interpreter_changing_nothing(
        self,
    ):
        assert _build_preflight_cmd("9.0.0") == [
            "uv",
            "pip",
            "install",
            "--dry-run",
            "--refresh-package",
            "fraisier",
            "--python",
            sys.executable,
            "fraisier==9.0.0",
        ]


class TestAnUpgradeThePythonCannotMeet:
    def test_the_install_is_never_attempted(
        self, tmp_path, monkeypatch, lock_dir, unit_dir
    ):
        _entry, calls = _fake_uv(tmp_path, monkeypatch, resolves=False)
        rc, send_restart = _upgrade(lock_dir)

        assert rc != 0
        send_restart.assert_not_called()
        assert "tool install" not in calls.read_text()

    def test_the_tool_is_left_exactly_as_it_was(
        self, tmp_path, monkeypatch, lock_dir, unit_dir
    ):
        """Guards the fixture too: a `tool install` here really does delete it."""
        entry, _ = _fake_uv(tmp_path, monkeypatch, resolves=False)
        _upgrade(lock_dir)
        assert entry.exists()

    def test_the_refusal_is_recorded_with_what_the_operator_must_do(
        self, tmp_path, monkeypatch, lock_dir, unit_dir
    ):
        _fake_uv(tmp_path, monkeypatch, resolves=False)
        _upgrade(lock_dir)

        record = read_self_upgrade_failure(lock_dir)
        assert record is not None
        assert record.required == "9.0.0"
        assert "requires Python >=3.14" in record.detail
        assert "uv tool install --force --python" in record.detail
        assert "fraisier==9.0.0" in record.detail


class TestAnUpgradeThePythonCanMeet:
    def test_the_install_runs_after_the_preflight(
        self, tmp_path, monkeypatch, lock_dir, unit_dir
    ):
        _, calls = _fake_uv(tmp_path, monkeypatch, resolves=True)
        _upgrade(lock_dir)

        verbs = [line.split()[0:2] for line in calls.read_text().splitlines()]
        assert verbs == [["pip", "install"], ["tool", "install"]]

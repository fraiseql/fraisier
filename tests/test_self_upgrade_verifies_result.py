"""The worker checks its own result before declaring success (#351).

`uv tool install --force` removes before it verifies. When it fails partway the
tool venv is left half-removed — `bin/` gone, `lib/` intact — and the binary the
webhook unit names in `ExecStart=` no longer resolves.

At that moment the *worst* thing the worker can do is request a restart: the
running process is the only working fraisier left on the host, and restarting it
turns a latent problem into an outage. So the worker resolves the unit's own
entrypoint after the install and refuses the restart when it does not resolve,
recording why.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from fraisier.self_upgrade_record import read_self_upgrade_failure
from fraisier.webhook_self_upgrade import ENTRYPOINT_BROKEN_RC, _run_upgrade

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


def _install_entrypoint(bin_dir: Path, name: str = "fraisier-webhook") -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    p = bin_dir / name
    p.write_text("#!/bin/sh\necho ok\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def _write_unit(unit_dir: Path, target: Path) -> None:
    (unit_dir / SERVICE).write_text(
        f"[Unit]\nDescription=webhook\n\n[Service]\nExecStart={target}\n"
    )


class TestARealFailedInstall:
    """Driven with a fake `uv` on PATH, not a stubbed `_run_install`.

    The defect is what the worker does *after* the install, and an install that
    is mocked out cannot leave the venv in the state that matters.
    """

    @pytest.fixture
    def fake_uv(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "tools" / "bin"
        entry = _install_entrypoint(bin_dir)
        shim_dir = tmp_path / "shim"
        shim_dir.mkdir()
        uv = shim_dir / "uv"
        # Exactly the #351 shape: bin/ is removed, then the install fails.
        uv.write_text(
            "#!/bin/sh\n"
            f"rm -rf '{bin_dir}'\n"
            "echo 'error: failed to remove directory ...: Permission denied' >&2\n"
            "exit 2\n"
        )
        uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
        return entry

    def test_no_restart_is_requested_and_the_breakage_is_recorded(
        self, lock_dir, unit_dir, fake_uv
    ):
        _write_unit(unit_dir, fake_uv)
        with patch("fraisier.webhook_self_upgrade._send_restart") as send_restart:
            rc = _run_upgrade(
                "0.62.0",
                SERVICE,
                "/run/fraisier/systemctl.sock",
                lock_dir=lock_dir,
                drain_settle_s=0,
            )

        send_restart.assert_not_called()
        assert rc != 0
        record = read_self_upgrade_failure(lock_dir)
        assert record is not None
        assert record.required == "0.62.0"
        assert "Permission denied" in record.detail

    def test_the_entrypoint_really_is_gone(self, lock_dir, unit_dir, fake_uv):
        """Guards the fixture: if `uv` stopped removing bin/, the tests above
        would pass for the wrong reason."""
        _write_unit(unit_dir, fake_uv)
        assert fake_uv.exists()
        with patch("fraisier.webhook_self_upgrade._send_restart"):
            _run_upgrade(
                "0.62.0",
                SERVICE,
                "/run/fraisier/systemctl.sock",
                lock_dir=lock_dir,
                drain_settle_s=0,
            )
        assert not fake_uv.exists()


class TestASuccessfulInstallThatLeftNothingBehind:
    """rc == 0 is not proof. The install can report success and still not resolve."""

    def test_a_broken_entrypoint_blocks_the_restart(self, lock_dir, unit_dir, tmp_path):
        missing = tmp_path / "tools" / "bin" / "fraisier-webhook"
        _write_unit(unit_dir, missing)
        with (
            patch("fraisier.webhook_self_upgrade._run_install", return_value=0),
            patch("fraisier.webhook_self_upgrade._send_restart") as send_restart,
            patch("fraisier.webhook_self_upgrade._wait_for_deploys_to_drain") as drain,
        ):
            drain.return_value = type("R", (), {"drained": True, "held": []})()
            rc = _run_upgrade(
                "0.62.0",
                SERVICE,
                "/run/fraisier/systemctl.sock",
                lock_dir=lock_dir,
                drain_settle_s=0,
            )

        send_restart.assert_not_called()
        assert rc == ENTRYPOINT_BROKEN_RC
        assert read_self_upgrade_failure(lock_dir) is not None

    def test_a_resolvable_entrypoint_still_restarts(self, lock_dir, unit_dir, tmp_path):
        """The regression guard: the happy path must not become cautious."""
        entry = _install_entrypoint(tmp_path / "tools" / "bin")
        _write_unit(unit_dir, entry)
        with (
            patch("fraisier.webhook_self_upgrade._run_install", return_value=0),
            patch(
                "fraisier.webhook_self_upgrade._send_restart", return_value=0
            ) as send_restart,
            patch("fraisier.webhook_self_upgrade._wait_for_deploys_to_drain") as drain,
        ):
            drain.return_value = type("R", (), {"drained": True, "held": []})()
            rc = _run_upgrade(
                "0.62.0",
                SERVICE,
                "/run/fraisier/systemctl.sock",
                lock_dir=lock_dir,
                drain_settle_s=0,
            )

        send_restart.assert_called_once()
        assert rc == 0


class TestWhenTheUnitCannotBeRead:
    """Abstaining is the safe direction: never block a restart on a guess."""

    def test_an_absent_unit_file_does_not_block_the_restart(
        self, lock_dir, unit_dir, tmp_path
    ):
        with (
            patch("fraisier.webhook_self_upgrade._run_install", return_value=0),
            patch(
                "fraisier.webhook_self_upgrade._send_restart", return_value=0
            ) as send_restart,
            patch("fraisier.webhook_self_upgrade._wait_for_deploys_to_drain") as drain,
        ):
            drain.return_value = type("R", (), {"drained": True, "held": []})()
            rc = _run_upgrade(
                "0.62.0",
                SERVICE,
                "/run/fraisier/systemctl.sock",
                lock_dir=lock_dir,
                drain_settle_s=0,
            )

        send_restart.assert_called_once()
        assert rc == 0


class TestTheResolver:
    def test_it_reads_the_units_exec_start(self, unit_dir, tmp_path):
        from fraisier.webhook_self_upgrade import _unit_entrypoint

        entry = _install_entrypoint(tmp_path / "bin")
        _write_unit(unit_dir, entry)
        assert _unit_entrypoint(SERVICE) == str(entry)

    def test_an_unknown_unit_resolves_to_none(self, unit_dir):
        from fraisier.webhook_self_upgrade import _unit_entrypoint

        assert _unit_entrypoint("fraisier-nope.service") is None

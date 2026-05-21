"""Tests for webhook self-upgrade runner (issue #162)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fraisier.webhook_self_upgrade import (
    _build_install_cmd,
    _run_upgrade,
    _spawn_upgrade,
    maybe_self_upgrade,
)


def _write_pyproject(tmp_path: Path, fraisier_pin: str | None) -> None:
    deps = "[]" if fraisier_pin is None else f'["fraisier=={fraisier_pin}"]'
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "myapp"\nversion = "1.0.0"\ndependencies = {deps}\n'
    )


class TestBuildInstallCmd:
    def test_matches_bootstrap_form(self):
        """The install command must mirror fraisier/bootstrap.py:221-222 exactly."""
        assert _build_install_cmd("0.16.6") == [
            "uv",
            "tool",
            "install",
            "--force",
            "--refresh-package",
            "fraisier",
            "fraisier==0.16.6",
        ]


class TestMaybeSelfUpgrade:
    def test_no_spawn_when_disabled(self, tmp_path):
        _write_pyproject(tmp_path, "99.0.0")
        spawn = MagicMock()
        maybe_self_upgrade(
            tmp_path, project_name="foo", enabled=False, spawn=spawn
        )
        spawn.assert_not_called()

    def test_no_spawn_when_required_is_none(self, tmp_path):
        # No pyproject.toml at all → detect returns None.
        spawn = MagicMock()
        maybe_self_upgrade(
            tmp_path, project_name="foo", enabled=True, spawn=spawn
        )
        spawn.assert_not_called()

    def test_no_spawn_when_required_equals_installed(self, tmp_path):
        _write_pyproject(tmp_path, "1.2.3")
        spawn = MagicMock()
        with patch(
            "fraisier.webhook_self_upgrade.importlib_metadata.version",
            return_value="1.2.3",
        ):
            maybe_self_upgrade(
                tmp_path, project_name="foo", enabled=True, spawn=spawn
            )
        spawn.assert_not_called()

    def test_no_spawn_when_required_older(self, tmp_path):
        _write_pyproject(tmp_path, "1.0.0")
        spawn = MagicMock()
        with patch(
            "fraisier.webhook_self_upgrade.importlib_metadata.version",
            return_value="1.2.3",
        ):
            maybe_self_upgrade(
                tmp_path, project_name="foo", enabled=True, spawn=spawn
            )
        spawn.assert_not_called()

    def test_spawn_when_required_newer(self, tmp_path):
        _write_pyproject(tmp_path, "2.0.0")
        spawn = MagicMock()
        with patch(
            "fraisier.webhook_self_upgrade.importlib_metadata.version",
            return_value="1.2.3",
        ):
            maybe_self_upgrade(
                tmp_path, project_name="foo", enabled=True, spawn=spawn
            )
        spawn.assert_called_once_with("2.0.0", "foo")

    def test_never_raises_on_internal_error(self, tmp_path):
        spawn = MagicMock(side_effect=RuntimeError("boom"))
        _write_pyproject(tmp_path, "99.0.0")
        # Must not raise even though spawn blows up.
        with patch(
            "fraisier.webhook_self_upgrade.importlib_metadata.version",
            return_value="1.2.3",
        ):
            maybe_self_upgrade(
                tmp_path, project_name="foo", enabled=True, spawn=spawn
            )

    def test_never_raises_on_malformed_installed_version(self, tmp_path):
        _write_pyproject(tmp_path, "1.2.3")
        spawn = MagicMock()
        with patch(
            "fraisier.webhook_self_upgrade.importlib_metadata.version",
            return_value="not-a-version",
        ):
            maybe_self_upgrade(
                tmp_path, project_name="foo", enabled=True, spawn=spawn
            )
        # Conservative: skip when we can't compare.
        spawn.assert_not_called()


class TestSpawnUpgrade:
    def test_spawn_uses_detached_kwargs(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "fraisier.webhook_self_upgrade._LOG_DIR", tmp_path / "logs"
        )
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", "/run/x.sock")
        with patch(
            "fraisier.webhook_self_upgrade.subprocess.Popen"
        ) as mock_popen:
            _spawn_upgrade("0.17.0", "myproj")
        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert cmd[0] == sys.executable
        assert "fraisier.webhook_self_upgrade" in cmd
        assert "--required" in cmd
        assert "0.17.0" in cmd
        assert "--service" in cmd
        assert "fraisier-myproj-webhook.service" in cmd
        assert "--socket" in cmd
        assert "/run/x.sock" in cmd
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.STDOUT
        # stdout should be a writable file handle, not None.
        assert kwargs["stdout"] is not None
        # Log directory was created on demand.
        assert (tmp_path / "logs").is_dir()

    def test_spawn_works_when_log_dir_uncreatable(self, monkeypatch):
        # Pointing _LOG_DIR at an unwritable path should not crash the spawn.
        monkeypatch.setattr(
            "fraisier.webhook_self_upgrade._LOG_DIR", Path("/dev/null/no")
        )
        monkeypatch.setenv("FRAISIER_SYSTEMCTL_SOCKET", "/run/x.sock")
        with patch(
            "fraisier.webhook_self_upgrade.subprocess.Popen"
        ) as mock_popen:
            _spawn_upgrade("0.17.0", "myproj")
        mock_popen.assert_called_once()
        _, kwargs = mock_popen.call_args
        # Falls back to DEVNULL when log file can't be opened.
        assert kwargs["stdout"] is subprocess.DEVNULL

    def test_spawn_with_no_socket_env(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "fraisier.webhook_self_upgrade._LOG_DIR", tmp_path / "logs"
        )
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_SOCKET", raising=False)
        with patch(
            "fraisier.webhook_self_upgrade.subprocess.Popen"
        ) as mock_popen:
            _spawn_upgrade("0.17.0", "myproj")
        args, _ = mock_popen.call_args
        cmd = args[0]
        # --socket is still passed (empty string) so the child can choose to skip.
        idx = cmd.index("--socket")
        assert cmd[idx + 1] == ""


class TestRunUpgrade:
    def test_install_success_triggers_restart_rpc(self):
        with patch(
            "fraisier.webhook_self_upgrade.subprocess.run"
        ) as mock_run, patch(
            "fraisier.webhook_self_upgrade._call_via_socket"
        ) as mock_socket:
            mock_run.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
            rc = _run_upgrade(
                "0.17.0", "fraisier-foo-webhook.service", "/run/x.sock"
            )
        assert rc == 0
        assert mock_run.call_args[0][0] == _build_install_cmd("0.17.0")
        mock_socket.assert_called_once_with(
            "/run/x.sock", "restart", "fraisier-foo-webhook.service"
        )

    def test_install_failure_skips_restart(self):
        with patch(
            "fraisier.webhook_self_upgrade.subprocess.run"
        ) as mock_run, patch(
            "fraisier.webhook_self_upgrade._call_via_socket"
        ) as mock_socket:
            mock_run.return_value = SimpleNamespace(
                returncode=1, stdout="", stderr="oops"
            )
            rc = _run_upgrade(
                "0.17.0", "fraisier-foo-webhook.service", "/run/x.sock"
            )
        assert rc == 1
        mock_socket.assert_not_called()

    def test_install_success_no_socket_logs_and_returns_ok(self, caplog):
        import logging

        with patch(
            "fraisier.webhook_self_upgrade.subprocess.run"
        ) as mock_run, patch(
            "fraisier.webhook_self_upgrade._call_via_socket"
        ) as mock_socket, caplog.at_level(
            logging.WARNING, logger="fraisier.webhook_self_upgrade"
        ):
            mock_run.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
            rc = _run_upgrade(
                "0.17.0", "fraisier-foo-webhook.service", ""
            )
        assert rc == 0
        mock_socket.assert_not_called()
        assert "FRAISIER_SYSTEMCTL_SOCKET" in caplog.text

    def test_restart_rpc_failure_returns_nonzero(self):
        with patch(
            "fraisier.webhook_self_upgrade.subprocess.run"
        ) as mock_run, patch(
            "fraisier.webhook_self_upgrade._call_via_socket",
            side_effect=ConnectionRefusedError("nope"),
        ):
            mock_run.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr=""
            )
            rc = _run_upgrade(
                "0.17.0", "fraisier-foo-webhook.service", "/run/x.sock"
            )
        assert rc != 0


@pytest.mark.parametrize("argv_socket", ["/run/x.sock", ""])
def test_main_entrypoint_invokes_run_upgrade(monkeypatch, argv_socket):
    """`python -m fraisier.webhook_self_upgrade` wires argv → _run_upgrade."""
    from fraisier import webhook_self_upgrade as mod

    captured = {}

    def fake_run(required, service, socket_path):
        captured["args"] = (required, service, socket_path)
        return 0

    monkeypatch.setattr(mod, "_run_upgrade", fake_run)
    argv = [
        "webhook_self_upgrade",
        "--required",
        "0.17.0",
        "--service",
        "fraisier-foo-webhook.service",
        "--socket",
        argv_socket,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as excinfo:
        mod._main()
    assert excinfo.value.code == 0
    assert captured["args"] == (
        "0.17.0",
        "fraisier-foo-webhook.service",
        argv_socket,
    )

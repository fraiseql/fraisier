"""Tests for SystemdServiceManager."""

import subprocess
from unittest.mock import MagicMock

import pytest

from fraisier.systemd import SystemdServiceManager


class TestRestart:
    """SystemdServiceManager.restart() calls systemctl restart via runner."""

    def test_restart_calls_systemctl_restart(self, monkeypatch):
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_WRAPPER", raising=False)
        runner = MagicMock()
        runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mgr = SystemdServiceManager(runner)

        mgr.restart("myapi")

        runner.run.assert_called_once_with(
            ["sudo", "systemctl", "restart", "myapi"],
            timeout=60,
            check=True,
        )

    def test_restart_with_custom_timeout(self, monkeypatch):
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_WRAPPER", raising=False)
        runner = MagicMock()
        runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mgr = SystemdServiceManager(runner)

        mgr.restart("myapi", timeout=120)

        runner.run.assert_called_once_with(
            ["sudo", "systemctl", "restart", "myapi"],
            timeout=120,
            check=True,
        )

    def test_restart_invalid_name_raises_valueerror(self):
        runner = MagicMock()
        mgr = SystemdServiceManager(runner)

        with pytest.raises(ValueError, match="Invalid service name"):
            mgr.restart("my;service")

        runner.run.assert_not_called()

    def test_restart_propagates_subprocess_error(self):
        runner = MagicMock()
        runner.run.side_effect = subprocess.CalledProcessError(1, "systemctl")
        mgr = SystemdServiceManager(runner)

        with pytest.raises(subprocess.CalledProcessError):
            mgr.restart("myapi")


class TestStatus:
    """SystemdServiceManager.status() returns parsed systemctl output."""

    def test_status_returns_active_state(self, monkeypatch):
        monkeypatch.delenv("FRAISIER_SYSTEMCTL_WRAPPER", raising=False)
        runner = MagicMock()
        runner.run.return_value = MagicMock(returncode=0, stdout="active\n", stderr="")
        mgr = SystemdServiceManager(runner)

        result = mgr.status("myapi")

        assert result == "active"
        runner.run.assert_called_once_with(
            ["sudo", "systemctl", "is-active", "myapi"],
            timeout=30,
            check=False,
        )

    def test_status_returns_inactive_state(self):
        runner = MagicMock()
        runner.run.return_value = MagicMock(
            returncode=3, stdout="inactive\n", stderr=""
        )
        mgr = SystemdServiceManager(runner)

        result = mgr.status("myapi")

        assert result == "inactive"

    def test_status_invalid_name_raises_valueerror(self):
        runner = MagicMock()
        mgr = SystemdServiceManager(runner)

        with pytest.raises(ValueError, match="Invalid service name"):
            mgr.status("bad|name")

        runner.run.assert_not_called()

"""Tests for BareMetalProvider subprocess-based SSH command execution."""

from unittest.mock import patch

import pytest

from fraisier.providers.bare_metal import BareMetalProvider


class TestRunCommand:
    """Test BareMetalProvider.run_command() executes commands via subprocess SSH."""

    def _make_provider(self, **overrides):
        config = {
            "host": "localhost",
            "port": 22,
            "username": "deploy",
            "key_path": "/home/deploy/.ssh/id_rsa",
            **overrides,
        }
        return BareMetalProvider(config)

    def test_run_command_returns_stdout_stderr_exit_code(self):
        """run_command() returns a (exit_code, stdout, stderr) tuple."""
        provider = self._make_provider()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "hello\n"
            mock_run.return_value.stderr = ""

            exit_code, stdout, stderr = provider.run_command("echo hello")

        assert exit_code == 0
        assert stdout == "hello\n"
        assert stderr == ""

    def test_run_command_builds_ssh_command_with_host_and_user(self):
        """run_command() shells out to ssh with correct user@host."""
        provider = self._make_provider(
            host="prod.example.com", username="deploy", port=22
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""

            provider.run_command("uptime")

        args = mock_run.call_args
        cmd = args[0][0]
        assert "ssh" in cmd
        assert "deploy@prod.example.com" in cmd
        assert "uptime" in cmd

    def test_run_command_uses_custom_port(self):
        """run_command() passes -p flag for non-default SSH port."""
        provider = self._make_provider(port=2222)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""

            provider.run_command("ls")

        cmd = mock_run.call_args[0][0]
        # Port should appear as -p 2222
        assert "-p" in cmd
        port_idx = cmd.index("-p")
        assert cmd[port_idx + 1] == "2222"

    def test_run_command_uses_key_path(self):
        """run_command() passes -i flag for SSH key."""
        provider = self._make_provider(key_path="/etc/ssh/deploy_key")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""

            provider.run_command("whoami")

        cmd = mock_run.call_args[0][0]
        assert "-i" in cmd
        key_idx = cmd.index("-i")
        assert cmd[key_idx + 1] == "/etc/ssh/deploy_key"

    def test_run_command_returns_nonzero_exit_code(self):
        """run_command() returns non-zero exit code without raising."""
        provider = self._make_provider()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = "command not found\n"

            exit_code, _stdout, stderr = provider.run_command("nonexistent")

        assert exit_code == 1
        assert stderr == "command not found\n"

    def test_run_command_respects_timeout(self):
        """run_command() passes timeout to subprocess.run."""
        provider = self._make_provider()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""

            provider.run_command("sleep 1", timeout=60)

        kwargs = mock_run.call_args[1]
        assert kwargs["timeout"] == 60

    def test_run_command_timeout_raises_runtime_error(self):
        """run_command() raises RuntimeError when subprocess times out."""
        import subprocess

        provider = self._make_provider()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=5)

            with pytest.raises(RuntimeError, match="timed out"):
                provider.run_command("long-running", timeout=5)

    def test_run_command_without_key_path(self):
        """run_command() works without explicit key_path (uses SSH defaults)."""
        provider = self._make_provider(key_path=None)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""

            provider.run_command("hostname")

        cmd = mock_run.call_args[0][0]
        assert "-i" not in cmd


class TestServiceManagement:
    """Test BareMetalProvider service management via run_command()."""

    def _make_provider(self, **overrides):
        config = {
            "host": "localhost",
            "port": 22,
            "username": "deploy",
            "key_path": "/home/deploy/.ssh/id_rsa",
            **overrides,
        }
        return BareMetalProvider(config)

    def test_start_service_calls_systemctl_start(self):
        """start_service() calls sudo systemctl start via run_command()."""
        provider = self._make_provider()

        with patch.object(provider, "run_command", return_value=(0, "", "")) as mock:
            result = provider.start_service("myapp")

        mock.assert_called_once_with("sudo systemctl start myapp.service", timeout=60)
        assert result is True

    def test_start_service_returns_false_on_failure(self):
        """start_service() returns False when systemctl exits non-zero."""
        provider = self._make_provider()

        with patch.object(
            provider, "run_command", return_value=(1, "", "Failed to start")
        ):
            result = provider.start_service("myapp")

        assert result is False

    def test_stop_service_calls_systemctl_stop(self):
        """stop_service() calls sudo systemctl stop via run_command()."""
        provider = self._make_provider()

        with patch.object(provider, "run_command", return_value=(0, "", "")) as mock:
            result = provider.stop_service("myapp")

        mock.assert_called_once_with("sudo systemctl stop myapp.service", timeout=60)
        assert result is True

    def test_stop_service_returns_false_on_failure(self):
        """stop_service() returns False when systemctl exits non-zero."""
        provider = self._make_provider()

        with patch.object(provider, "run_command", return_value=(1, "", "not loaded")):
            result = provider.stop_service("myapp")

        assert result is False

    def test_restart_service_calls_systemctl_restart(self):
        """restart_service() runs systemctl restart via run_command()."""
        provider = self._make_provider()

        with patch.object(provider, "run_command", return_value=(0, "", "")) as mock:
            result = provider.restart_service("myapp")

        mock.assert_called_once_with("sudo systemctl restart myapp.service", timeout=60)
        assert result is True

    def test_restart_service_returns_false_on_failure(self):
        """restart_service() returns False when systemctl exits non-zero."""
        provider = self._make_provider()

        with patch.object(
            provider, "run_command", return_value=(1, "", "unit not found")
        ):
            result = provider.restart_service("myapp")

        assert result is False

    def test_service_status_calls_systemctl_is_active(self):
        """service_status() runs systemctl is-active via run_command()."""
        provider = self._make_provider()

        with patch.object(
            provider, "run_command", return_value=(0, "active\n", "")
        ) as mock:
            result = provider.service_status("myapp")

        mock.assert_called_once_with("sudo systemctl is-active myapp.service")
        assert result == {"service": "myapp", "active": True, "state": "active"}

    def test_service_status_inactive(self):
        """service_status() reports inactive when systemctl returns non-zero."""
        provider = self._make_provider()

        with patch.object(provider, "run_command", return_value=(3, "inactive\n", "")):
            result = provider.service_status("myapp")

        assert result == {"service": "myapp", "active": False, "state": "inactive"}

    def test_service_status_failed(self):
        """service_status() reports failed state."""
        provider = self._make_provider()

        with patch.object(provider, "run_command", return_value=(3, "failed\n", "")):
            result = provider.service_status("myapp")

        assert result == {"service": "myapp", "active": False, "state": "failed"}

    def test_start_service_custom_timeout(self):
        """start_service() passes custom timeout to run_command()."""
        provider = self._make_provider()

        with patch.object(provider, "run_command", return_value=(0, "", "")) as mock:
            provider.start_service("myapp", timeout=120)

        mock.assert_called_once_with("sudo systemctl start myapp.service", timeout=120)

    def test_service_methods_raise_on_run_command_error(self):
        """Service methods propagate RuntimeError from run_command()."""
        provider = self._make_provider()

        with (
            patch.object(
                provider,
                "run_command",
                side_effect=RuntimeError("SSH timed out"),
            ),
            pytest.raises(RuntimeError, match="SSH timed out"),
        ):
            provider.start_service("myapp")


class TestRunCommandDefensiveFlags:
    """LB-4 regression: run_command() must carry the full defensive flag set.

    Phase 1 inventory item LB-4 — bare_metal previously hand-built its own
    ssh argv with only ``BatchMode`` and ``StrictHostKeyChecking``, missing
    ``ConnectTimeout``, ``AddressFamily``, and ``-n``. After migrating onto
    ``fraisier.ssh.short_cmd`` every flag must be present by construction.
    """

    def _make_provider(self, **overrides):
        config = {
            "host": "localhost",
            "port": 22,
            "username": "deploy",
            "key_path": "/home/deploy/.ssh/id_rsa",
            **overrides,
        }
        return BareMetalProvider(config)

    def _capture_ssh_argv(self, provider, command="true"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            provider.run_command(command)
        return mock_run.call_args[0][0]

    def test_run_command_includes_connect_timeout(self):
        """LB-4/LB-1: defensive ConnectTimeout must be present."""
        provider = self._make_provider()
        argv = self._capture_ssh_argv(provider)
        assert "ConnectTimeout=30" in argv

    def test_run_command_includes_dash_n(self):
        """LB-4/LB-2: -n must be set on the short-cmd pattern."""
        provider = self._make_provider()
        argv = self._capture_ssh_argv(provider)
        assert "-n" in argv

    def test_run_command_includes_batch_mode(self):
        """BatchMode=yes must remain present after migration."""
        provider = self._make_provider()
        argv = self._capture_ssh_argv(provider)
        assert "BatchMode=yes" in argv

    def test_run_command_honours_address_family(self):
        """LB-4/LB-3: AddressFamily must be threaded through when configured."""
        provider = self._make_provider(address_family="inet")
        argv = self._capture_ssh_argv(provider)
        assert "AddressFamily=inet" in argv

    def test_run_command_honours_custom_connect_timeout(self):
        """connect_timeout config knob must reach the ssh argv."""
        provider = self._make_provider(connect_timeout=10)
        argv = self._capture_ssh_argv(provider)
        assert "ConnectTimeout=10" in argv

    def test_run_command_omits_address_family_when_unset(self):
        """Default config should not pin AddressFamily."""
        provider = self._make_provider()
        argv = self._capture_ssh_argv(provider)
        assert not any("AddressFamily" in token for token in argv)

    def test_run_command_uses_accept_new_by_default(self):
        provider = self._make_provider()
        argv = self._capture_ssh_argv(provider)
        assert "StrictHostKeyChecking=accept-new" in argv

    def test_run_command_strict_host_key_off(self):
        provider = self._make_provider(strict_host_key=False)
        argv = self._capture_ssh_argv(provider)
        assert "StrictHostKeyChecking=no" in argv

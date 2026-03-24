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
        """start_service() runs 'systemctl start <name>.service' via run_command()."""
        provider = self._make_provider()

        with patch.object(provider, "run_command", return_value=(0, "", "")) as mock:
            result = provider.start_service("myapp")

        mock.assert_called_once_with("systemctl start myapp.service", timeout=60)
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
        """stop_service() runs 'systemctl stop <name>.service' via run_command()."""
        provider = self._make_provider()

        with patch.object(provider, "run_command", return_value=(0, "", "")) as mock:
            result = provider.stop_service("myapp")

        mock.assert_called_once_with("systemctl stop myapp.service", timeout=60)
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

        mock.assert_called_once_with("systemctl restart myapp.service", timeout=60)
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

        mock.assert_called_once_with("systemctl is-active myapp.service")
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

        mock.assert_called_once_with("systemctl start myapp.service", timeout=120)

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


class TestBuildSshCommand:
    """Test SSH command construction helper."""

    def _make_provider(self, **overrides):
        config = {
            "host": "localhost",
            "port": 22,
            "username": "deploy",
            "key_path": "/home/deploy/.ssh/id_rsa",
            **overrides,
        }
        return BareMetalProvider(config)

    def test_build_ssh_command_basic(self):
        """_build_ssh_command() returns list-form command."""
        provider = self._make_provider(
            host="server.example.com", username="deploy", port=22
        )
        cmd = provider._build_ssh_command("ls /var/app")

        assert cmd[0] == "ssh"
        assert "deploy@server.example.com" in cmd
        assert cmd[-1] == "ls /var/app"

    def test_build_ssh_command_disables_strict_host_checking_by_default(self):
        """_build_ssh_command() disables strict host key checking."""
        provider = self._make_provider()
        cmd = provider._build_ssh_command("date")

        assert "-o" in cmd
        host_check_idx = cmd.index("StrictHostKeyChecking=no")
        # The -o flag should precede it
        assert cmd[host_check_idx - 1] == "-o"

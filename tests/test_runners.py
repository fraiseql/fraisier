"""Tests for fraisier.runners — command runner abstraction."""

import subprocess
from unittest.mock import patch

from fraisier.runners import (
    CommandRunner,
    LocalRunner,
    SSHRunner,
    runner_from_config,
)


class TestLocalRunner:
    """Tests for LocalRunner."""

    def test_implements_protocol(self):
        assert isinstance(LocalRunner(), CommandRunner)

    def test_run_wraps_subprocess(self):
        runner = LocalRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["echo", "hi"], returncode=0, stdout="hi\n", stderr=""
            )
            result = runner.run(["echo", "hi"])

        assert result.stdout == "hi\n"
        mock_run.assert_called_once_with(
            ["echo", "hi"],
            cwd=None,
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )

    def test_run_passes_cwd(self):
        runner = LocalRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["ls"], returncode=0, stdout="", stderr=""
            )
            runner.run(["ls"], cwd="/tmp")

        mock_run.assert_called_once_with(
            ["ls"],
            cwd="/tmp",
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )


class TestSSHRunner:
    """Tests for SSHRunner."""

    def test_implements_protocol(self):
        runner = SSHRunner(host="example.com")
        assert isinstance(runner, CommandRunner)

    def test_builds_ssh_command_default(self):
        runner = SSHRunner(host="deploy.example.com", user="fraisier")
        prefix = runner._build_ssh_prefix()

        assert prefix[0] == "ssh"
        assert "-o" in prefix
        assert "StrictHostKeyChecking=accept-new" in prefix
        assert "BatchMode=yes" in prefix
        assert "-p" in prefix
        assert "22" in prefix
        assert "fraisier@deploy.example.com" in prefix

    def test_builds_ssh_command_with_key(self):
        runner = SSHRunner(host="h", user="u", key_path="/path/to/key")
        prefix = runner._build_ssh_prefix()
        assert "-i" in prefix
        assert "/path/to/key" in prefix

    def test_builds_ssh_command_strict_host_key_off(self):
        runner = SSHRunner(host="h", user="u", strict_host_key=False)
        prefix = runner._build_ssh_prefix()
        assert "StrictHostKeyChecking=no" in prefix

    def test_run_routes_through_ssh(self):
        runner = SSHRunner(host="h", user="u", port=2222)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ok", stderr=""
            )
            runner.run(["sudo", "systemctl", "restart", "api"])

        called_cmd = mock_run.call_args[0][0]
        assert called_cmd[0] == "ssh"
        assert "u@h" in called_cmd
        assert "-p" in called_cmd
        # The remote command should be a single string
        remote = called_cmd[-1]
        assert "sudo" in remote
        assert "systemctl" in remote
        assert "restart" in remote
        assert "api" in remote

    def test_run_with_cwd_prepends_cd(self):
        runner = SSHRunner(host="h", user="u")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["ls"], cwd="/var/www/app")

        remote = mock_run.call_args[0][0][-1]
        assert remote.startswith("cd ")
        assert "/var/www/app" in remote
        assert "ls" in remote

    def test_run_custom_port(self):
        runner = SSHRunner(host="h", user="u", port=2222)
        prefix = runner._build_ssh_prefix()
        idx = prefix.index("-p")
        assert prefix[idx + 1] == "2222"


class TestRunnerFromConfig:
    """Tests for runner_from_config factory."""

    def test_returns_local_runner_when_no_ssh(self):
        runner = runner_from_config(None)
        assert isinstance(runner, LocalRunner)

    def test_returns_ssh_runner_when_ssh_config(self):
        runner = runner_from_config(
            {
                "host": "deploy.example.com",
                "user": "fraisier",
                "port": 22,
                "key_path": "~/.ssh/deploy_key",
            }
        )
        assert isinstance(runner, SSHRunner)
        assert runner.host == "deploy.example.com"
        assert runner.user == "fraisier"
        assert runner.key_path == "~/.ssh/deploy_key"

    def test_ssh_runner_defaults(self):
        runner = runner_from_config({"host": "h"})
        assert isinstance(runner, SSHRunner)
        assert runner.user == "root"
        assert runner.port == 22
        assert runner.strict_host_key is True


class TestDeployerUsesRunner:
    """Integration: deployers route commands through the runner."""

    def test_api_deployer_restart_uses_runner(self):
        from fraisier.deployers.api import APIDeployer

        mock_runner = LocalRunner()
        deployer = APIDeployer(
            {
                "app_path": "/var/www/api",
                "systemd_service": "api.service",
            },
            runner=mock_runner,
        )
        assert deployer.runner is mock_runner

    def test_etl_deployer_uses_runner(self):
        from fraisier.deployers.etl import ETLDeployer

        mock_runner = LocalRunner()
        deployer = ETLDeployer(
            {"app_path": "/var/etl"},
            runner=mock_runner,
        )
        assert deployer.runner is mock_runner

    def test_scheduled_deployer_uses_runner(self):
        from fraisier.deployers.scheduled import ScheduledDeployer

        mock_runner = LocalRunner()
        deployer = ScheduledDeployer(
            {"systemd_service": "backup.service"},
            runner=mock_runner,
        )
        assert deployer.runner is mock_runner

    def test_default_runner_is_local(self):
        from fraisier.deployers.api import APIDeployer

        deployer = APIDeployer({"app_path": "/var/www/api"})
        assert isinstance(deployer.runner, LocalRunner)

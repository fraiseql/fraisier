"""Tests for fraisier.runners — command runner abstraction."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

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
            env=None,
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
            env=None,
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

    def test_run_prepends_safe_path(self):
        runner = SSHRunner(host="h", user="u")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ok", stderr=""
            )
            runner.run(["usermod", "-aG", "www-data", "deploy"])

        remote = mock_run.call_args[0][0][-1]
        assert remote.startswith("PATH=")
        assert "/usr/local/sbin" in remote
        assert "/usr/sbin" in remote
        assert "/sbin" in remote
        assert "usermod" in remote

    def test_run_with_env_merges_with_safe_path(self):
        runner = SSHRunner(host="h", user="u")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["echo", "hi"], env={"FOO": "bar"})

        remote = mock_run.call_args[0][0][-1]
        assert "PATH=" in remote
        assert "FOO=bar" in remote

    def test_run_with_env_path_overrides_safe_default(self):
        runner = SSHRunner(host="h", user="u")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["echo"], env={"PATH": "/custom/bin"})

        remote = mock_run.call_args[0][0][-1]
        assert "PATH=/custom/bin" in remote

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

    def test_ssh_options_shared_with_scp(self):
        runner = SSHRunner(host="h", user="u", key_path="/id_ed25519")
        opts = runner._build_ssh_options()
        assert "StrictHostKeyChecking=accept-new" in opts
        assert "BatchMode=yes" in opts
        assert "-i" in opts
        assert "/id_ed25519" in opts
        # No host or port in options (those are caller's responsibility)
        assert "u@h" not in opts
        assert "-p" not in opts

    def test_upload_builds_scp_command(self, tmp_path):
        runner = SSHRunner(host="prod.example.com", user="root", port=22)
        local_file = tmp_path / "fraises.yaml"
        local_file.write_text("name: test\n")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.upload(local_file, "/opt/fraisier/fraises.yaml")

        called = mock_run.call_args[0][0]
        assert called[0] == "scp"
        assert "-P" in called
        assert "22" in called
        assert str(local_file) in called
        assert "root@prod.example.com:/opt/fraisier/fraises.yaml" in called

    def test_upload_uses_port_capital_P(self, tmp_path):
        runner = SSHRunner(host="h", user="u", port=2222)
        local_file = tmp_path / "f.txt"
        local_file.write_text("")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.upload(local_file, "/tmp/f.txt")

        called = mock_run.call_args[0][0]
        idx = called.index("-P")
        assert called[idx + 1] == "2222"

    def test_upload_tree_pipes_tar_over_ssh(self, tmp_path):
        runner = SSHRunner(host="h", user="u")
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("hello")

        fake_ssh = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("subprocess.run") as mock_run,
        ):
            mock_proc = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"", b"")
            mock_popen.return_value = mock_proc
            mock_run.return_value = fake_ssh

            runner.upload_tree(src, "/tmp/remote")

        tar_cmd = mock_popen.call_args[0][0]
        assert tar_cmd[0] == "tar"
        assert str(src) in tar_cmd

        ssh_cmd = mock_run.call_args[0][0]
        assert ssh_cmd[0] == "ssh"
        assert any("/tmp/remote" in str(part) for part in ssh_cmd)

    def test_upload_tree_raises_on_tar_failure(self, tmp_path):
        runner = SSHRunner(host="h", user="u")
        src = tmp_path / "src"
        src.mkdir()

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("subprocess.run") as mock_run,
        ):
            mock_proc = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.returncode = 1
            mock_proc.communicate.return_value = (b"", b"tar error")
            mock_popen.return_value = mock_proc
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"", stderr=b""
            )

            with pytest.raises(subprocess.CalledProcessError):
                runner.upload_tree(src, "/tmp/remote")

    def test_upload_tree_raises_on_ssh_failure(self, tmp_path):
        runner = SSHRunner(host="h", user="u")
        src = tmp_path / "src"
        src.mkdir()

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("subprocess.run") as mock_run,
        ):
            mock_proc = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"", b"")
            mock_popen.return_value = mock_proc
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=255, stdout=b"", stderr=b"ssh: connection refused"
            )

            with pytest.raises(subprocess.CalledProcessError):
                runner.upload_tree(src, "/tmp/remote")


class TestSSHRunnerSudo:
    """Tests for SSHRunner with use_sudo=True."""

    def test_run_wraps_command_in_sudo(self):
        runner = SSHRunner(host="h", user="u", use_sudo=True)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["useradd", "--system", "deploy"])

        remote = mock_run.call_args[0][0][-1]
        assert remote.startswith("sudo sh -c ")
        assert "useradd" in remote
        assert "PATH=" in remote

    def test_run_without_sudo_does_not_wrap(self):
        runner = SSHRunner(host="h", user="u", use_sudo=False)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["useradd", "--system", "deploy"])

        remote = mock_run.call_args[0][0][-1]
        assert not remote.startswith("sudo ")

    def test_run_sudo_with_cwd(self):
        runner = SSHRunner(host="h", user="u", use_sudo=True)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["ls"], cwd="/opt/app")

        remote = mock_run.call_args[0][0][-1]
        assert remote.startswith("sudo sh -c ")
        assert "cd" in remote
        assert "/opt/app" in remote

    def test_upload_sudo_uses_temp_path(self, tmp_path):
        runner = SSHRunner(host="h", user="u", use_sudo=True)
        local_file = tmp_path / "fraises.yaml"
        local_file.write_text("name: test\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.upload(local_file, "/opt/fraisier/fraises.yaml")

        # First call: scp to temp path
        scp_call = mock_run.call_args_list[0][0][0]
        assert scp_call[0] == "scp"
        assert any("/tmp/.fraisier-upload-fraises.yaml" in str(a) for a in scp_call)
        # Second call: sudo mv to final path
        mv_call = mock_run.call_args_list[1][0][0]
        assert mv_call[0] == "ssh"
        remote_cmd = mv_call[-1]
        assert "sudo sh -c" in remote_cmd
        assert "mv" in remote_cmd
        assert "/opt/fraisier/fraises.yaml" in remote_cmd

    def test_upload_no_sudo_scps_directly(self, tmp_path):
        runner = SSHRunner(host="h", user="u", use_sudo=False)
        local_file = tmp_path / "fraises.yaml"
        local_file.write_text("name: test\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.upload(local_file, "/opt/fraisier/fraises.yaml")

        # Only one call: scp directly to target
        assert mock_run.call_count == 1
        scp_call = mock_run.call_args[0][0]
        assert "u@h:/opt/fraisier/fraises.yaml" in scp_call

    def test_upload_tree_sudo_wraps_remote_cmd(self, tmp_path):
        runner = SSHRunner(host="h", user="u", use_sudo=True)
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("hello")

        fake_ssh = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )

        with (
            patch("subprocess.Popen") as mock_popen,
            patch("subprocess.run") as mock_run,
        ):
            mock_proc = MagicMock()
            mock_proc.stdout = MagicMock()
            mock_proc.returncode = 0
            mock_proc.communicate.return_value = (b"", b"")
            mock_popen.return_value = mock_proc
            mock_run.return_value = fake_ssh

            runner.upload_tree(src, "/tmp/remote")

        ssh_cmd = mock_run.call_args[0][0]
        remote_cmd = ssh_cmd[-1]
        assert remote_cmd.startswith("sudo sh -c ")
        assert "mkdir -p" in remote_cmd
        assert "tar xzf" in remote_cmd


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

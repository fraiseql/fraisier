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

        # The remote command is shell-quoted by ssh.short_cmd (single token);
        # the remote sh -c strips those quotes. Assert content, not prefix.
        remote = mock_run.call_args[0][0][-1]
        assert "PATH=" in remote
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
        # `cd` precedes the env exports inside the (now shell-quoted)
        # remote command string.
        assert "cd " in remote
        assert "/var/www/app" in remote
        assert "ls" in remote

    def test_run_custom_port(self):
        """Port from config must reach the ssh argv (-p flag)."""
        runner = SSHRunner(host="h", user="u", port=2222)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["true"])
        called_cmd = mock_run.call_args[0][0]
        p_idx = called_cmd.index("-p")
        assert called_cmd[p_idx + 1] == "2222"

    # --- LB-1/LB-2/LB-3 regression tests ---
    #
    # These tests guard against the latent bugs documented in
    # .phases/2026-04-10-ssh-io-contract/latent-bugs.md. SSHRunner.run
    # routes through the fraisier.ssh entry points which carry the full
    # defensive flag set (BatchMode, ConnectTimeout, AddressFamily,
    # StrictHostKeyChecking, -n) by construction.

    def test_run_includes_connect_timeout(self):
        """LB-1: SSHRunner.run was missing ConnectTimeout, leaving every
        deployer command vulnerable to the IPv6-fallback hang fixed in
        cli/logs.py by commit 4dd1927."""
        runner = SSHRunner(host="h", user="u")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["true"])
        called_cmd = mock_run.call_args[0][0]
        assert "-o" in called_cmd
        assert "ConnectTimeout=30" in called_cmd

    def test_run_includes_dash_n(self):
        """LB-2: short-cmd pattern requires -n so ssh never allocates a
        stdin channel."""
        runner = SSHRunner(host="h", user="u")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["true"])
        called_cmd = mock_run.call_args[0][0]
        assert "-n" in called_cmd

    def test_run_honours_address_family(self):
        """LB-3: AddressFamily must be threaded through from config so
        operators can pin IPv4/IPv6."""
        runner = SSHRunner(host="h", user="u", address_family="inet")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["true"])
        called_cmd = mock_run.call_args[0][0]
        assert "AddressFamily=inet" in called_cmd

    def test_run_honours_custom_connect_timeout(self):
        runner = SSHRunner(host="h", user="u", connect_timeout=5)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["true"])
        called_cmd = mock_run.call_args[0][0]
        assert "ConnectTimeout=5" in called_cmd

    def test_runner_from_config_threads_connect_timeout(self):
        runner = runner_from_config(
            {"host": "h", "connect_timeout": 7, "address_family": "inet6"}
        )
        assert isinstance(runner, SSHRunner)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["true"])
        called_cmd = mock_run.call_args[0][0]
        assert "ConnectTimeout=7" in called_cmd
        assert "AddressFamily=inet6" in called_cmd

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

    def test_upload_includes_full_defensive_flag_set(self, tmp_path):
        """LB-7: scp accepts the same -o flags as ssh and was missing
        ConnectTimeout/AddressFamily, leaving uploads vulnerable to the
        same IPv6-fallback hang as ssh. The shared scp_options helper
        carries the full defensive flag set."""
        runner = SSHRunner(
            host="h", user="u", port=2222, address_family="inet"
        )
        local_file = tmp_path / "f.txt"
        local_file.write_text("")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.upload(local_file, "/tmp/f.txt")

        called = mock_run.call_args[0][0]
        assert called[0] == "scp"
        assert "ConnectTimeout=30" in called
        assert "BatchMode=yes" in called
        assert "AddressFamily=inet" in called
        assert "StrictHostKeyChecking=accept-new" in called
        # scp uses -P (capital) not -p
        assert "-P" in called
        assert "-p" not in called
        assert "-n" not in called

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

    def test_upload_tree_includes_connect_timeout_and_address_family(
        self, tmp_path
    ):
        """LB-5: the upload_tree path was missing ConnectTimeout and
        AddressFamily, so an IPv6-broken host hung the very first
        deploy step (scaffold upload). Routing through ssh.data_pipe
        carries the defensive flag set by construction."""
        runner = SSHRunner(host="h", user="u", address_family="inet")
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
                args=[], returncode=0, stdout=b"", stderr=b""
            )

            runner.upload_tree(src, "/tmp/remote")

        ssh_cmd = mock_run.call_args[0][0]
        assert ssh_cmd[0] == "ssh"
        assert "ConnectTimeout=30" in ssh_cmd
        assert "AddressFamily=inet" in ssh_cmd
        # data_pipe must NOT pass -n: tar stream is on stdin.
        assert "-n" not in ssh_cmd

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
        # ssh.short_cmd shell-quotes the remote command into a single
        # token; the remote sh -c strips that outer quoting before
        # executing. Assert content rather than exact prefix.
        assert "sudo sh -c " in remote
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
        assert "sudo " not in remote

    def test_run_sudo_with_cwd(self):
        runner = SSHRunner(host="h", user="u", use_sudo=True)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["ls"], cwd="/opt/app")

        remote = mock_run.call_args[0][0][-1]
        assert "sudo sh -c " in remote
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
        # ssh.data_pipe shell-joins the remote command into a single token;
        # the remote sh -c strips the outer quoting before executing.
        assert "sudo sh -c " in remote_cmd
        assert "mkdir -p" in remote_cmd
        assert "tar xzf" in remote_cmd


class TestSSHRunnerSudoPassword:
    """Tests for SSHRunner with sudo_password (sudo -S via stdin)."""

    def test_run_uses_sudo_s_when_password_set(self):
        runner = SSHRunner(host="h", user="u", use_sudo=True, sudo_password="secret")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["useradd", "--system", "deploy"])

        remote = mock_run.call_args[0][0][-1]
        assert "sudo -S sh -c" in remote

    def test_run_pipes_password_via_stdin(self):
        runner = SSHRunner(host="h", user="u", use_sudo=True, sudo_password="secret")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["echo", "hi"])

        kwargs = mock_run.call_args[1]
        assert kwargs["input"] == "secret\n"
        assert kwargs["capture_output"] is True

    def test_run_without_password_uses_plain_sudo(self):
        runner = SSHRunner(host="h", user="u", use_sudo=True)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["echo", "hi"])

        remote = mock_run.call_args[0][0][-1]
        assert "sudo sh -c" in remote
        assert "sudo -S" not in remote
        kwargs = mock_run.call_args[1]
        assert kwargs.get("capture_output") is True

    def test_upload_sudo_password_pipes_to_mv(self, tmp_path):
        runner = SSHRunner(host="h", user="u", use_sudo=True, sudo_password="secret")
        local_file = tmp_path / "fraises.yaml"
        local_file.write_text("name: test\n")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.upload(local_file, "/opt/fraisier/fraises.yaml")

        # The mv call (second call) should use sudo -S and pipe password
        mv_call = mock_run.call_args_list[1]
        remote_cmd = mv_call[0][0][-1]
        assert "sudo -S sh -c" in remote_cmd
        kwargs = mv_call[1]
        assert kwargs["input"] == "secret\n"

    def test_upload_tree_sudo_password_uses_two_step(self, tmp_path):
        runner = SSHRunner(host="h", user="u", use_sudo=True, sudo_password="secret")
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("hello")

        fake_ssh = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b""
        )
        fake_ssh_text = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
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
            mock_run.side_effect = [fake_ssh, fake_ssh_text]

            runner.upload_tree(src, "/tmp/remote")

        # First call: tar to temp dir (no sudo)
        first_ssh = mock_run.call_args_list[0][0][0]
        first_remote = first_ssh[-1]
        assert "sudo" not in first_remote
        assert "/tmp/.fraisier-upload-tree" in first_remote

        # Second call: sudo -S mv into place
        second_ssh = mock_run.call_args_list[1][0][0]
        second_remote = second_ssh[-1]
        assert "sudo -S sh -c" in second_remote
        second_kwargs = mock_run.call_args_list[1][1]
        assert second_kwargs["input"] == "secret\n"


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

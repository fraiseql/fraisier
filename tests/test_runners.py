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


#: An ``ssh`` that does what sshd does with the words after ``user@host``:
#: joins them with spaces and hands the line to the login shell's ``-c``.
#: It never unquotes; a fake that did would hide #494 again.
_SSHD_MIRROR = """#!/bin/sh
printf '%s\\n' "$@" > "$(dirname "$0")/argv"
while [ $# -gt 0 ]; do
    case $1 in
        -n) exec </dev/null; shift ;;
        -o|-i|-p) shift 2 ;;
        *) shift; break ;;
    esac
done
exec sh -c "$*"
"""


#: A ``sudo`` that keeps real sudo's contract for the flags ``SSHRunner``
#: uses: ``-S`` reads one line and refuses anything but ``secret`` (rc 1),
#: ``-v`` leaves a ticket, ``-n`` without a ticket refuses. It runs the
#: command as the caller and logs each invocation's flags.
_SUDO_MIRROR = """#!/bin/sh
here=$(dirname "$0")
printf '%s\\n' "$*" >> "$here/sudo.log"
while [ $# -gt 0 ]; do
    case $1 in
        -S) IFS= read -r pw
            [ "$pw" = secret ] || { echo "1 incorrect password attempt" >&2; exit 1; }
            : > "$here/ticket"; shift ;;
        -p) shift 2 ;;
        -n) [ -e "$here/ticket" ] || { echo "a password is required" >&2; exit 1; }
            shift ;;
        -v) exit 0 ;;
        *) break ;;
    esac
done
exec "$@"
"""


@pytest.fixture
def sshd_mirror(tmp_path, monkeypatch):
    """Put :data:`_SSHD_MIRROR` and :data:`_SUDO_MIRROR` first on ``PATH``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, script in (("ssh", _SSHD_MIRROR), ("sudo", _SUDO_MIRROR)):
        fake = bin_dir / name
        fake.write_text(script)
        fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return bin_dir


class TestSSHRunnerRunsTheCommand:
    """The command reaches the remote shell as its words, not one quoted word.

    Every ``SSHRunner`` call exited 127 from v0.8.0 to v0.94.0 (#494) while
    the tests above, which read the command string, stayed green.
    """

    def test_run_runs_the_command(self, sshd_mirror):
        result = SSHRunner(host="h", user="u").run(["echo", "hi"], timeout=10)

        assert result.stdout == "hi\n"

    def test_run_preserves_argument_boundaries(self, sshd_mirror):
        words = ["a b", "c'd", '"e"', "$HOME", "f;g"]

        result = SSHRunner(host="h", user="u").run(
            ["printf", "%s|", *words], timeout=10
        )

        assert result.stdout == "".join(f"{w}|" for w in words)

    def test_run_in_cwd(self, sshd_mirror, tmp_path):
        cwd = tmp_path / "dir with space"
        cwd.mkdir()

        result = SSHRunner(host="h", user="u").run(["pwd"], cwd=str(cwd), timeout=10)

        assert result.stdout == f"{cwd}\n"

    def test_run_passes_env_to_the_command(self, sshd_mirror):
        result = SSHRunner(host="h", user="u").run(
            ["printenv", "FRAISIER_WIRE"], env={"FRAISIER_WIRE": "v a'l"}, timeout=10
        )

        assert result.stdout == "v a'l\n"

    def test_env_value_reaches_the_command_but_not_the_local_argv(self, sshd_mirror):
        result = SSHRunner(host="h", user="u").run(
            ["printenv", "PGPASSWORD"], env={"PGPASSWORD": "unit-s3cr3t"}, timeout=10
        )

        assert result.stdout == "unit-s3cr3t\n"
        assert "unit-s3cr3t" not in (sshd_mirror / "argv").read_text()

    def test_safe_path_reaches_the_command(self, sshd_mirror):
        result = SSHRunner(host="h", user="u").run(["printenv", "PATH"], timeout=10)

        assert result.stdout == f"{SSHRunner._SAFE_PATH}\n"

    def test_env_and_safe_path_both_reach_the_command(self, sshd_mirror):
        result = SSHRunner(host="h", user="u").run(
            ["sh", "-c", 'printf "%s|%s" "$FOO" "$PATH"'],
            env={"FOO": "bar"},
            timeout=10,
        )

        assert result.stdout == f"bar|{SSHRunner._SAFE_PATH}"

    def test_env_path_overrides_the_safe_path(self, sshd_mirror):
        result = SSHRunner(host="h", user="u").run(
            ["printenv", "PATH"], env={"PATH": "/usr/bin:/bin:/custom"}, timeout=10
        )

        assert result.stdout == "/usr/bin:/bin:/custom\n"

    def test_command_stdin_is_at_eof(self, sshd_mirror):
        """LB-2's guarantee, kept without ``-n``.

        ``-n`` stopped ssh from holding stdin open (da5c119). ``run()`` now
        feeds the env payload on stdin, and ``subprocess.run(input=...)``
        closes the pipe after it, so the command still reads EOF.
        """
        result = SSHRunner(host="h", user="u").run(["cat"], env={"K": "v"}, timeout=10)

        assert result.stdout == ""

    @pytest.mark.parametrize(
        "password", [None, "secret"], ids=["no-password", "sudo-password"]
    )
    def test_stdin_without_the_sentinel_fails_loudly_and_runs_nothing(
        self, sshd_mirror, password
    ):
        """The reader exits 125 at EOF rather than running with a partial env.

        The payload is built by ``run()``, so this drives the remote script
        directly with what a truncated stdin would leave of it.
        """
        runner = SSHRunner(
            host="h", user="u", use_sudo=bool(password), sudo_password=password
        )
        with patch("fraisier.runners._env_payload", return_value="export K=v\n"):
            result = runner.run(["echo", "ran"], check=False, timeout=10)

        assert (result.returncode, result.stdout) == (125, "")

    @pytest.mark.parametrize("key", ["A;B", "1A", "", "A B", "A=B", "A\nB"])
    def test_env_key_that_is_not_a_shell_name_is_refused(self, key):
        with pytest.raises(ValueError, match="environment variable name"):
            SSHRunner(host="h", user="u").run(["true"], env={key: "x"})

    def test_upload_tree_writes_every_file(self, sshd_mirror, tmp_path):
        src = tmp_path / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("alpha")
        (src / "sub" / "b.txt").write_text("beta")
        dest = tmp_path / "dest with space"

        SSHRunner(host="h", user="u").upload_tree(src, str(dest))

        assert (dest / "a.txt").read_text() == "alpha"
        assert (dest / "sub" / "b.txt").read_text() == "beta"


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
    # StrictHostKeyChecking) by construction. LB-2's -n is retired for run(),
    # whose stdin carries the env payload: see test_command_stdin_is_at_eof.

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
        runner = SSHRunner(host="h", user="u", port=2222, address_family="inet")
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

    def test_upload_tree_includes_connect_timeout_and_address_family(self, tmp_path):
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
        assert "sudo sh -c " in remote
        assert "useradd" in remote

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

    def test_run_checks_the_password_then_runs_with_the_ticket(self, sshd_mirror):
        runner = SSHRunner(host="h", user="u", use_sudo=True, sudo_password="secret")

        result = runner.run(["printenv", "K"], env={"K": "v"}, timeout=10)

        assert result.stdout == "v\n"
        log = (sshd_mirror / "sudo.log").read_text().splitlines()
        assert [line.split(" sh ")[0] for line in log] == ["-S -p  -v", "-n"]

    def test_run_refuses_a_wrong_password_before_reading_env(self, sshd_mirror):
        runner = SSHRunner(host="h", user="u", use_sudo=True, sudo_password="nope")

        result = runner.run(["printenv", "K"], env={"K": "v"}, check=False, timeout=10)

        assert (result.returncode, result.stdout) == (124, "")
        assert (sshd_mirror / "sudo.log").read_text().count("\n") == 1

    def test_run_pipes_password_via_stdin(self):
        runner = SSHRunner(host="h", user="u", use_sudo=True, sudo_password="secret")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            runner.run(["echo", "hi"])

        kwargs = mock_run.call_args[1]
        assert kwargs["input"].startswith("secret\n__FRAISIER_ENV__\n")
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

        # The mv call (second call) carries the password ahead of the env
        mv_call = mock_run.call_args_list[1]
        assert "/opt/fraisier/fraises.yaml" in mv_call[0][0][-1]
        kwargs = mv_call[1]
        assert kwargs["input"].startswith("secret\n__FRAISIER_ENV__\n")

    def test_upload_tree_sudo_password_uses_two_step(self, sshd_mirror, tmp_path):
        runner = SSHRunner(host="h", user="u", use_sudo=True, sudo_password="secret")
        src = tmp_path / "src"
        src.mkdir()
        (src / "file.txt").write_text("hello")
        dest = tmp_path / "dest"

        runner.upload_tree(src, str(dest))

        assert (dest / "file.txt").read_text() == "hello"
        # The tar leg ran without sudo; only the copy into place used it.
        log = (sshd_mirror / "sudo.log").read_text().splitlines()
        assert [line.split(" sh ")[0] for line in log] == ["-S -p  -v", "-n"]


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


class TestScrubPg:
    """``scrub_pg=True`` keeps ambient ``PG*`` away from a libpq child.

    A runner child still sees the environment it runs in: ours for
    ``LocalRunner``, the remote login's for ``SSHRunner``. A connection URL
    names its target fully, so every ambient ``PG*`` but the operator defaults
    is dropped there, before *env* is applied.
    """

    @pytest.fixture(params=["local", "ssh"])
    def runner(self, request, monkeypatch):
        monkeypatch.setenv("PGUSER", "acme")
        monkeypatch.setenv("PGOPTIONS", "-c acme.x=1")
        monkeypatch.setenv("PGSSLROOTCERT", "/etc/acme/root.crt")
        if request.param == "local":
            return LocalRunner()
        request.getfixturevalue("sshd_mirror")
        return SSHRunner(host="h", user="u")

    @staticmethod
    def _env(runner, **kwargs) -> dict[str, str]:
        out = runner.run(["env"], timeout=10, **kwargs).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def test_ambient_pg_variables_are_dropped(self, runner):
        env = self._env(runner, scrub_pg=True)

        assert "PGUSER" not in env
        assert "PGOPTIONS" not in env

    def test_an_operator_default_is_kept(self, runner):
        assert self._env(runner, scrub_pg=True)["PGSSLROOTCERT"] == "/etc/acme/root.crt"

    def test_env_is_applied_after_the_scrub(self, runner):
        env = self._env(
            runner,
            scrub_pg=True,
            env={"PGPASSWORD": "pw", "PGSSLROOTCERT": "/etc/acme/url.crt"},
        )

        assert (env["PGPASSWORD"], env["PGSSLROOTCERT"]) == ("pw", "/etc/acme/url.crt")

    def test_other_variables_are_untouched(self, runner, monkeypatch):
        monkeypatch.setenv("FRAISIER_KEPT", "yes")

        assert self._env(runner, scrub_pg=True)["FRAISIER_KEPT"] == "yes"

    def test_without_it_the_environment_is_as_before(self, runner):
        assert self._env(runner)["PGUSER"] == "acme"

    def test_an_ambient_service_refuses_a_local_child(self, monkeypatch):
        """``PgConn.check_environment``'s rule; the remote login's ``PGSERVICE``
        is the remote host's, and the remote scrub drops it."""
        monkeypatch.setenv("PGSERVICE", "acme")

        done = LocalRunner().run(["env"], check=False, scrub_pg=True)

        assert done.returncode == 123
        assert "PGSERVICE is set in the environment" in done.stderr


_TCP_PG_ENV = {"PGHOST": "db.acme.test", "PGDATABASE": "acme_db"}


class TestScrubPgFailsClosedOnTransportPolicy:
    """An ambient TLS or GSS policy the scrub would drop refuses the command
    instead: the child would connect without what the operator meant.
    The same rule as ``PgConn.child_env``, on the environment the child runs
    in: ours for ``LocalRunner``, the remote login's for ``SSHRunner``."""

    @pytest.fixture(params=["local", "ssh"])
    def runner(self, request, monkeypatch):
        monkeypatch.setenv("PGSSLMODE", "verify-full")
        if request.param == "local":
            return LocalRunner()
        request.getfixturevalue("sshd_mirror")
        return SSHRunner(host="h", user="u")

    def test_a_tcp_child_without_the_policy_is_refused(self, runner):
        with pytest.raises(subprocess.CalledProcessError) as caught:
            runner.run(["true"], timeout=10, env=_TCP_PG_ENV, scrub_pg=True)

        assert caught.value.returncode == 123
        assert "PGSSLMODE" in caught.value.stderr
        assert "sslmode=verify-full" in caught.value.stderr

    def test_unchecked_it_returns_the_refusal(self, runner):
        result = runner.run(
            ["true"], timeout=10, env=_TCP_PG_ENV, scrub_pg=True, check=False
        )

        assert result.returncode == 123

    def test_a_url_that_pins_it_runs(self, runner):
        env = {**_TCP_PG_ENV, "PGSSLMODE": "disable"}

        assert runner.run(["true"], timeout=10, env=env, scrub_pg=True).returncode == 0

    @pytest.mark.parametrize("host", [None, "/run/postgresql", "@acme"])
    def test_a_socket_child_runs(self, runner, host):
        env = {"PGDATABASE": "acme_db"} | ({"PGHOST": host} if host else {})

        assert runner.run(["true"], timeout=10, env=env, scrub_pg=True).returncode == 0

    def test_without_scrub_nothing_is_checked(self, runner):
        assert runner.run(["true"], timeout=10, env=_TCP_PG_ENV).returncode == 0

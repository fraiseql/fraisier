"""Unit tests for PreflightChecker."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from fraisier.preflight import CheckResult, PreflightChecker, PreflightResult
from fraisier.runners import SSHRunner

_OK = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _err(stderr: str = "error") -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, "cmd", stderr=stderr)


@pytest.fixture
def mock_runner():
    runner = MagicMock(spec=SSHRunner)
    runner.run.return_value = _OK
    runner.host = "prod.example.com"
    runner.user = "root"
    runner.port = 22
    return runner


@pytest.fixture
def checker(mock_runner):
    return PreflightChecker(runner=mock_runner, deploy_user="fraisier")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_passed_defaults(self):
        r = CheckResult(name="test", passed=True)
        assert r.message == ""
        assert r.fix_hint == ""

    def test_failed_with_hint(self):
        r = CheckResult(name="test", passed=False, fix_hint="do this")
        assert r.passed is False
        assert r.fix_hint == "do this"


class TestPreflightResult:
    def test_passed_when_all_pass(self):
        r = PreflightResult(
            server="example.com",
            checks=[
                CheckResult(name="a", passed=True),
                CheckResult(name="b", passed=True),
            ],
        )
        assert r.passed is True
        assert r.failed_count == 0

    def test_failed_when_any_fails(self):
        r = PreflightResult(
            server="example.com",
            checks=[
                CheckResult(name="a", passed=True),
                CheckResult(name="b", passed=False),
            ],
        )
        assert r.passed is False
        assert r.failed_count == 1

    def test_failed_count_multiple(self):
        r = PreflightResult(
            server="example.com",
            checks=[
                CheckResult(name="a", passed=False),
                CheckResult(name="b", passed=False),
                CheckResult(name="c", passed=True),
            ],
        )
        assert r.failed_count == 2


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


class TestCheckSSH:
    def test_passes_when_echo_succeeds(self, checker, mock_runner):
        mock_runner.run.return_value = _ok("ok")
        result = checker.check_ssh()
        assert result.passed is True
        assert "SSH" in result.name or "ssh" in result.name.lower()

    def test_fails_when_connection_refused(self, checker, mock_runner):
        mock_runner.run.side_effect = _err("Connection refused")
        result = checker.check_ssh()
        assert result.passed is False
        assert "Connection refused" in result.message

    def test_fails_on_timeout(self, checker, mock_runner):
        mock_runner.run.side_effect = subprocess.TimeoutExpired("ssh", 10)
        result = checker.check_ssh()
        assert result.passed is False


class TestCheckSudo:
    def test_passes_when_passwordless_sudo(self, checker, mock_runner):
        mock_runner.run.return_value = _ok()
        result = checker.check_sudo()
        assert result.passed is True

    def test_fails_when_no_sudo(self, checker, mock_runner):
        mock_runner.run.side_effect = _err("sudo: a password is required")
        result = checker.check_sudo()
        assert result.passed is False
        assert result.fix_hint != ""


class TestCheckSudoPasswordless:
    def test_passes_when_passwordless(self, checker, mock_runner):
        mock_runner.run.return_value = _ok()
        result = checker.check_sudo_passwordless()
        assert result.passed is True

    def test_fails_when_password_required(self, checker, mock_runner):
        mock_runner.run.side_effect = _err("sudo: a password is required")
        result = checker.check_sudo_passwordless()
        assert result.passed is False
        assert "visudo" in result.fix_hint or "sudoers" in result.fix_hint


class TestCheckPackage:
    def test_passes_when_installed(self, checker, mock_runner):
        mock_runner.run.return_value = _ok("/usr/bin/git")
        result = checker.check_package("git")
        assert result.passed is True

    def test_fails_when_missing(self, checker, mock_runner):
        mock_runner.run.side_effect = _err()
        result = checker.check_package("nginx")
        assert result.passed is False
        assert "nginx" in result.fix_hint


class TestCheckDiskSpace:
    def test_passes_with_enough_space(self, checker, mock_runner):
        mock_runner.run.return_value = _ok(
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1        50G   30G   18G  63% /\n"
        )
        result = checker.check_disk_space()
        assert result.passed is True
        assert "18G" in result.message or "18" in result.message

    def test_fails_with_low_space(self, checker, mock_runner):
        mock_runner.run.return_value = _ok(
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1        50G   49G    1G  98% /\n"
        )
        result = checker.check_disk_space()
        assert result.passed is False

    def test_fails_on_parse_error(self, checker, mock_runner):
        mock_runner.run.return_value = _ok("unexpected output")
        result = checker.check_disk_space()
        assert result.passed is False


class TestCheckPorts:
    def test_passes_when_ports_free(self, checker, mock_runner):
        # ss output with no matching ports
        mock_runner.run.return_value = _ok(
            "State  Recv-Q Send-Q Local Address:Port\nLISTEN 0      128    0.0.0.0:22\n"
        )
        result = checker.check_ports([8000, 8001])
        assert result.passed is True

    def test_fails_when_port_in_use(self, checker, mock_runner):
        mock_runner.run.return_value = _ok(
            "State  Recv-Q Send-Q Local Address:Port\n"
            "LISTEN 0      128    0.0.0.0:8000\n"
            "LISTEN 0      128    0.0.0.0:8001\n"
        )
        result = checker.check_ports([8000, 8001])
        assert result.passed is False
        assert "8000" in result.message or "8001" in result.message


# ---------------------------------------------------------------------------
# Full preflight run
# ---------------------------------------------------------------------------


class TestRunAll:
    def test_collects_all_results(self, checker, mock_runner):
        """run_all() should return results for every check, not abort early."""
        mock_runner.run.return_value = _ok(
            "Filesystem      Size  Used Avail Use% Mounted on\n"
            "/dev/sda1        50G   30G   18G  63% /\n"
        )
        result = checker.run_all()
        assert isinstance(result, PreflightResult)
        assert result.server == "prod.example.com"
        # At minimum: ssh, sudo, passwordless, git, nginx, disk
        assert len(result.checks) >= 6

    def test_continues_after_failure(self, checker, mock_runner):
        """Even when SSH fails, other checks are still attempted."""
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise subprocess.CalledProcessError(
                    1, "ssh", stderr="Connection refused"
                )
            return _ok(
                "Filesystem      Size  Used Avail Use% Mounted on\n"
                "/dev/sda1        50G   30G   18G  63% /\n"
            )

        mock_runner.run.side_effect = side_effect
        result = checker.run_all()
        # Should have more than 1 check even though first failed
        assert len(result.checks) >= 6

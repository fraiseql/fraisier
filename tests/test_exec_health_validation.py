"""Tests for ExecHealthChecker command validation."""

import pytest

from fraisier.health_check import ExecHealthChecker


class TestExecHealthCheckerValidation:
    """ExecHealthChecker rejects commands with shell metacharacters."""

    def test_clean_command_accepted(self):
        checker = ExecHealthChecker("/usr/bin/curl -sf http://localhost/health")
        assert checker.command == "/usr/bin/curl -sf http://localhost/health"

    def test_semicolon_rejected(self):
        with pytest.raises(ValueError, match="metacharacter"):
            ExecHealthChecker("curl http://localhost; rm -rf /")

    def test_pipe_rejected(self):
        with pytest.raises(ValueError, match="metacharacter"):
            ExecHealthChecker("curl http://localhost | grep ok")

    def test_ampersand_rejected(self):
        with pytest.raises(ValueError, match="metacharacter"):
            ExecHealthChecker("curl http://localhost && echo done")

    def test_backtick_rejected(self):
        with pytest.raises(ValueError, match="metacharacter"):
            ExecHealthChecker("curl `hostname`")

    def test_dollar_rejected(self):
        with pytest.raises(ValueError, match="metacharacter"):
            ExecHealthChecker("curl $HOST")

    def test_empty_command_rejected(self):
        with pytest.raises(ValueError, match="Empty command"):
            ExecHealthChecker("")

    def test_shell_mode_skips_validation(self):
        """When shell=True is explicitly set, skip validation (operator knows)."""
        checker = ExecHealthChecker("curl http://localhost | grep ok", shell=True)
        assert checker.use_shell is True

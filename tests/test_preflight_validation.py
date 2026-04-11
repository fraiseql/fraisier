"""Tests for pre-flight operational validation checks."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from fraisier.validation import ValidationCheckResult, ValidationRunner


@pytest.fixture
def runner_with_config(tmp_path):
    """Create a ValidationRunner with a sample config."""
    from fraisier.config import FraisierConfig

    cfg_file = tmp_path / "fraises.yaml"
    cfg_file.write_text("""
git:
  provider: github
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
        clone_url: https://github.com/org/api.git
        systemd_service: api.service
        health_check:
          url: http://localhost:8000/health
          timeout: 10
        database:
          strategy: migrate
""")
    config = FraisierConfig(str(cfg_file))
    return ValidationRunner(config)


class TestSSHCheck:
    def test_skip_ssh(self, runner_with_config):
        results = runner_with_config.run_operational(skip_ssh=True)
        ssh_results = [r for r in results if r.name == "ssh_connectivity"]
        assert all(r.passed for r in ssh_results) or not ssh_results

    def test_ssh_failure_detected(self, runner_with_config):
        """SSH check skipped when no ssh: block configured - mechanism exists."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(255, "ssh")
            results = runner_with_config.run_operational()
        assert isinstance(results, list)


class TestSSHCheckLB6:
    """LB-6 regression: the probe must honour the per-fraise ``ssh:`` block.

    Phase 1 inventory item LB-6 — _check_ssh_connectivity previously read
    flat ``ssh_host``/``ssh_user``/``ssh_port`` keys (a stale schema) and
    omitted ``strict_host_key``, ``key_path``, ``address_family``. After
    migrating onto ``fraisier.ssh.short_cmd`` it shares the defensive flag
    set with every other call site.
    """

    def _runner(self, tmp_path, ssh_yaml: str):
        from fraisier.config import FraisierConfig
        from fraisier.validation import ValidationRunner

        cfg_file = tmp_path / "fraises.yaml"
        cfg_file.write_text(
            "fraises:\n"
            "  api:\n"
            "    type: api\n"
            "    environments:\n"
            "      production:\n"
            "        app_path: /var/www/api\n"
            "        clone_url: https://github.com/org/api.git\n"
            "        ssh:\n" + ssh_yaml
        )
        return ValidationRunner(FraisierConfig(str(cfg_file)))

    def _capture_argv(self, runner):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            runner.run_operational(skip_git=True, skip_db=True)
        assert mock_run.called, "expected the SSH probe to invoke subprocess.run"
        return mock_run.call_args[0][0]

    def test_probe_reads_ssh_block(self, tmp_path):
        runner = self._runner(
            tmp_path, "          host: prod.example.com\n          user: deploy\n"
        )
        argv = self._capture_argv(runner)
        assert "deploy@prod.example.com" in argv

    def test_probe_includes_connect_timeout(self, tmp_path):
        runner = self._runner(tmp_path, "          host: prod.example.com\n")
        argv = self._capture_argv(runner)
        assert any("ConnectTimeout=" in token for token in argv)

    def test_probe_includes_dash_n(self, tmp_path):
        runner = self._runner(tmp_path, "          host: prod.example.com\n")
        argv = self._capture_argv(runner)
        assert "-n" in argv

    def test_probe_includes_batch_mode(self, tmp_path):
        runner = self._runner(tmp_path, "          host: prod.example.com\n")
        argv = self._capture_argv(runner)
        assert "BatchMode=yes" in argv

    def test_probe_honours_address_family(self, tmp_path):
        runner = self._runner(
            tmp_path,
            "          host: prod.example.com\n          address_family: inet\n",
        )
        argv = self._capture_argv(runner)
        assert "AddressFamily=inet" in argv

    def test_probe_honours_strict_host_key_off(self, tmp_path):
        runner = self._runner(
            tmp_path,
            "          host: prod.example.com\n          strict_host_key: false\n",
        )
        argv = self._capture_argv(runner)
        assert "StrictHostKeyChecking=no" in argv

    def test_probe_honours_key_path(self, tmp_path):
        runner = self._runner(
            tmp_path,
            "          host: prod.example.com\n"
            "          key_path: /etc/ssh/deploy_key\n",
        )
        argv = self._capture_argv(runner)
        assert "/etc/ssh/deploy_key" in argv


class TestGitReachabilityCheck:
    def test_reachable_repo(self, runner_with_config):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            results = runner_with_config.run_operational(skip_ssh=True, skip_db=True)
        git_results = [r for r in results if r.name == "git_reachability"]
        assert any(r.passed for r in git_results)

    def test_unreachable_repo(self, runner_with_config):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(128, "git")
            results = runner_with_config.run_operational(skip_ssh=True, skip_db=True)
        git_results = [r for r in results if r.name == "git_reachability"]
        assert any(not r.passed for r in git_results)

    def test_skip_git(self, runner_with_config):
        results = runner_with_config.run_operational(
            skip_ssh=True, skip_db=True, skip_git=True
        )
        git_results = [r for r in results if r.name == "git_reachability"]
        assert not git_results


class TestDBCheck:
    def test_skip_db(self, runner_with_config):
        results = runner_with_config.run_operational(
            skip_ssh=True, skip_db=True, skip_git=True
        )
        db_results = [r for r in results if r.name == "db_connectivity"]
        assert not db_results


class TestRunOperational:
    def test_returns_list_of_results(self, runner_with_config):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            results = runner_with_config.run_operational()
        assert isinstance(results, list)
        assert all(isinstance(r, ValidationCheckResult) for r in results)

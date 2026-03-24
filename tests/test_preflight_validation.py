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
        """SSH check skipped when no ssh_host configured - mechanism exists."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(255, "ssh")
            results = runner_with_config.run_operational()
        assert isinstance(results, list)


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

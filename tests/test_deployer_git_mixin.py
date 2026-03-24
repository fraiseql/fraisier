"""Tests for GitDeployMixin and deployers using bare repo pattern."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from fraisier.deployers.base import DeploymentStatus
from fraisier.deployers.etl import ETLDeployer
from fraisier.deployers.scheduled import ScheduledDeployer


class TestGitDeployMixin:
    """Tests for common git-deploy logic shared across deployers."""

    def test_etl_deployer_has_bare_repo_path(self):
        """Mixin sets up bare_repo from repos_base + fraise_name."""
        config = {
            "fraise_name": "pipeline",
            "app_path": "/var/etl",
            "repos_base": "/var/lib/fraisier/repos",
        }
        deployer = ETLDeployer(config)
        assert deployer.bare_repo == Path("/var/lib/fraisier/repos/pipeline.git")

    def test_scheduled_deployer_has_bare_repo_path(self):
        """Mixin sets up bare_repo for ScheduledDeployer too."""
        config = {
            "fraise_name": "backup",
            "app_path": "/var/backup",
            "repos_base": "/var/lib/fraisier/repos",
        }
        deployer = ScheduledDeployer(config)
        assert deployer.bare_repo == Path("/var/lib/fraisier/repos/backup.git")

    def test_get_current_version_uses_worktree_sha(self, mock_subprocess):
        """Mixin delegates get_current_version to get_worktree_sha."""
        mock_subprocess.return_value = MagicMock(
            stdout="abc123def456abcd\n", returncode=0
        )
        deployer = ETLDeployer({"app_path": "/var/etl"})
        version = deployer.get_current_version()
        assert version == "abc123de"

    def test_get_current_version_none_without_app_path(self):
        """No app_path means no version."""
        deployer = ETLDeployer({})
        assert deployer.get_current_version() is None


class TestETLDeployerBareRepo:
    """Tests for ETLDeployer using bare repo pattern."""

    def test_execute_clones_and_fetches_via_bare_repo(self):
        """ETLDeployer.execute() uses clone_bare_repo + fetch_and_checkout."""
        config = {
            "fraise_name": "pipeline",
            "app_path": "/var/etl",
            "clone_url": "git@github.com:org/etl.git",
            "repos_base": "/tmp/repos",
            "script_path": "scripts/run.py",
        }
        deployer = ETLDeployer(config)

        with (
            patch(
                "fraisier.deployers.mixins.clone_bare_repo",
            ) as mock_clone,
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa111", "bbb222"),
            ) as mock_fc,
            patch(
                "fraisier.deployers.mixins.write_status",
            ),
            patch("subprocess.run") as mock_run,
        ):
            # Mock the script execution to succeed
            mock_run.return_value = MagicMock(returncode=0, stdout="ok\n")
            result = deployer.execute()

        mock_clone.assert_called_once_with(
            "git@github.com:org/etl.git",
            Path("/tmp/repos/pipeline.git"),
        )
        mock_fc.assert_called_once_with(
            Path("/tmp/repos/pipeline.git"),
            Path("/var/etl"),
            "main",
        )
        assert result.success is True
        assert result.status == DeploymentStatus.SUCCESS

    def test_execute_runs_configured_script(self):
        """After git pull, ETLDeployer runs the configured script."""
        config = {
            "fraise_name": "pipeline",
            "app_path": "/var/etl",
            "script_path": "scripts/run.py",
            "repos_base": "/tmp/repos",
        }
        deployer = ETLDeployer(config)

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok\n")
            deployer.execute()

        # Should have called the script
        calls = mock_run.call_args_list
        script_calls = [c for c in calls if "scripts/run.py" in str(c)]
        assert len(script_calls) == 1

    def test_execute_fails_when_script_fails(self):
        """ETLDeployer fails when configured script returns non-zero."""
        from subprocess import CalledProcessError

        config = {
            "fraise_name": "pipeline",
            "app_path": "/var/etl",
            "script_path": "scripts/run.py",
            "repos_base": "/tmp/repos",
        }
        deployer = ETLDeployer(config)

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = CalledProcessError(1, "python scripts/run.py")
            result = deployer.execute()

        assert result.success is False
        assert result.status == DeploymentStatus.FAILED

    def test_execute_without_script_just_pulls(self):
        """ETLDeployer without script_path just does git pull."""
        config = {
            "fraise_name": "pipeline",
            "app_path": "/var/etl",
            "repos_base": "/tmp/repos",
        }
        deployer = ETLDeployer(config)

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
        ):
            result = deployer.execute()

        assert result.success is True

    def test_execute_stores_previous_sha_for_rollback(self):
        """ETLDeployer stores old SHA for rollback."""
        config = {
            "fraise_name": "pipeline",
            "app_path": "/var/etl",
            "repos_base": "/tmp/repos",
        }
        deployer = ETLDeployer(config)

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old_sha_123", "new_sha_456"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
        ):
            deployer.execute()

        assert deployer._previous_sha == "old_sha_123"

    def test_rollback_uses_bare_repo(self, mock_subprocess):
        """ETLDeployer rollback checks out previous SHA via bare repo."""
        config = {
            "fraise_name": "pipeline",
            "app_path": "/var/etl",
            "repos_base": "/tmp/repos",
        }
        deployer = ETLDeployer(config)
        deployer._previous_sha = "abc123def456"
        mock_subprocess.return_value = MagicMock(stdout="version\n", returncode=0)

        with patch("fraisier.deployers.mixins.write_status"):
            result = deployer.rollback()

        assert result.success is True
        assert result.status == DeploymentStatus.ROLLED_BACK
        calls = mock_subprocess.call_args_list
        assert any("abc123def456" in str(c) for c in calls)


class TestScheduledDeployerBareRepo:
    """Tests for ScheduledDeployer using bare repo pattern."""

    def test_execute_pulls_code_then_updates_timer(self):
        """ScheduledDeployer pulls code via bare repo, then enables timer."""
        config = {
            "fraise_name": "backup",
            "app_path": "/var/backup",
            "clone_url": "git@github.com:org/backup.git",
            "repos_base": "/tmp/repos",
            "systemd_timer": "backup.timer",
        }
        deployer = ScheduledDeployer(config)

        with (
            patch(
                "fraisier.deployers.mixins.clone_bare_repo",
            ) as mock_clone,
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ) as mock_fc,
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="timer:active\n")
            result = deployer.execute()

        mock_clone.assert_called_once()
        mock_fc.assert_called_once()
        assert result.success is True

        # Should call enable, start, daemon-reload for the timer
        calls = [str(c) for c in mock_run.call_args_list]
        assert any("enable" in c for c in calls)
        assert any("start" in c for c in calls)
        assert any("daemon-reload" in c for c in calls)

    def test_execute_without_clone_url_skips_clone(self):
        """Without clone_url, skip the clone step but still fetch."""
        config = {
            "fraise_name": "backup",
            "app_path": "/var/backup",
            "repos_base": "/tmp/repos",
            "systemd_timer": "backup.timer",
        }
        deployer = ScheduledDeployer(config)

        with (
            patch(
                "fraisier.deployers.mixins.clone_bare_repo",
            ) as mock_clone,
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="timer:active\n")
            deployer.execute()

        mock_clone.assert_not_called()

    def test_execute_without_app_path_skips_git(self):
        """Without app_path, skip git operations entirely (timer-only deploy)."""
        config = {
            "fraise_name": "backup",
            "systemd_timer": "backup.timer",
        }
        deployer = ScheduledDeployer(config)

        with (
            patch(
                "fraisier.deployers.mixins.clone_bare_repo",
            ) as mock_clone,
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
            ) as mock_fc,
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="timer:active\n")
            result = deployer.execute()

        mock_clone.assert_not_called()
        mock_fc.assert_not_called()
        assert result.success is True

    def test_execute_writes_status_file(self):
        """ScheduledDeployer writes status file at deploy and success."""
        config = {
            "fraise_name": "backup",
            "app_path": "/var/backup",
            "repos_base": "/tmp/repos",
            "systemd_timer": "backup.timer",
        }
        deployer = ScheduledDeployer(config)

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ),
            patch("fraisier.deployers.mixins.write_status") as mock_ws,
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="timer:active\n")
            deployer.execute()

        # Should write "deploying" then "success"
        states = [call.args[0].state for call in mock_ws.call_args_list]
        assert "deploying" in states
        assert "success" in states

    def test_rollback_restarts_timer_and_writes_status(self, mock_subprocess):
        """ScheduledDeployer rollback restarts timer and writes status."""
        config = {
            "fraise_name": "backup",
            "systemd_timer": "backup.timer",
        }
        deployer = ScheduledDeployer(config)
        mock_subprocess.return_value = MagicMock(stdout="timer:active\n", returncode=0)

        with patch("fraisier.deployers.mixins.write_status") as mock_ws:
            result = deployer.rollback()

        assert result.success is True
        assert result.status == DeploymentStatus.ROLLED_BACK

        calls = [str(c) for c in mock_subprocess.call_args_list]
        assert any("restart" in c for c in calls)

        # Should write rolled_back status
        states = [call.args[0].state for call in mock_ws.call_args_list]
        assert "rolled_back" in states

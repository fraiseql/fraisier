"""Tests for rollback across all deployer types."""

from unittest.mock import MagicMock, patch

from fraisier.deployers.base import DeploymentStatus
from fraisier.deployers.docker_compose import DockerComposeDeployer
from fraisier.deployers.etl import ETLDeployer
from fraisier.deployers.scheduled import ScheduledDeployer


class TestETLRollback:
    def test_rollback_restores_previous_sha(self):
        config = {
            "fraise_name": "pipeline",
            "environment": "prod",
            "app_path": "/tmp/etl",
            "branch": "main",
        }
        runner = MagicMock()
        deployer = ETLDeployer(config, runner=runner)
        deployer._previous_sha = "abc123def456"

        with patch.object(deployer, "_write_status"):
            result = deployer.rollback()

        assert result.success
        assert result.status == DeploymentStatus.ROLLED_BACK
        assert result.new_version == "abc123de"
        # Should have run git checkout
        git_calls = [c for c in runner.run.call_args_list if "checkout" in str(c)]
        assert len(git_calls) >= 1

    def test_rollback_no_previous_sha_fails(self):
        config = {
            "fraise_name": "pipeline",
            "environment": "prod",
            "app_path": "/tmp/etl",
            "branch": "main",
        }
        runner = MagicMock()
        deployer = ETLDeployer(config, runner=runner)
        deployer._previous_sha = None

        with patch.object(deployer, "_write_status"):
            result = deployer.rollback()

        assert not result.success


class TestScheduledRollback:
    def test_rollback_restores_sha_and_restarts_timer(self):
        config = {
            "fraise_name": "backup",
            "environment": "prod",
            "app_path": "/tmp/sched",
            "branch": "main",
            "systemd_timer": "backup.timer",
            "systemd_service": "backup.service",
        }
        runner = MagicMock()
        deployer = ScheduledDeployer(config, runner=runner)
        deployer._previous_sha = "abc123def456"

        with patch.object(deployer, "_write_status"):
            result = deployer.rollback()

        assert result.success
        assert result.status == DeploymentStatus.ROLLED_BACK
        # Should have run git checkout AND timer restart
        all_calls = [str(c) for c in runner.run.call_args_list]
        assert any("checkout" in c for c in all_calls)
        assert any("restart" in c or "start" in c for c in all_calls)


class TestDockerComposeRollback:
    def test_rollback_uses_previous_tag(self):
        config = {
            "fraise_name": "web",
            "environment": "prod",
            "service_name": "api",
            "compose_file": "docker-compose.yml",
            "image_tag": "v2.0",
        }
        runner = MagicMock()
        deployer = DockerComposeDeployer(config, runner=runner)
        deployer._previous_tag = "v1.0"

        result = deployer.rollback()

        assert result.success
        assert result.status == DeploymentStatus.ROLLED_BACK

    def test_rollback_no_previous_tag_fails(self):
        config = {
            "fraise_name": "web",
            "environment": "prod",
            "compose_file": "docker-compose.yml",
        }
        runner = MagicMock()
        deployer = DockerComposeDeployer(config, runner=runner)
        deployer._previous_tag = None

        result = deployer.rollback()

        assert not result.success

    def test_rollback_to_explicit_version(self):
        config = {
            "fraise_name": "web",
            "environment": "prod",
            "compose_file": "docker-compose.yml",
        }
        runner = MagicMock()
        deployer = DockerComposeDeployer(config, runner=runner)

        result = deployer.rollback(to_version="v0.9")

        assert result.success

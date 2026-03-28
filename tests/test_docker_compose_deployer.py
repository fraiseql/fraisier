"""Tests for DockerComposeDeployer."""

import json
import subprocess
from unittest.mock import MagicMock

from fraisier.deployers.base import DeploymentStatus
from fraisier.deployers.docker_compose import DockerComposeDeployer


def _make_deployer(**overrides):
    config = {
        "compose_file": "docker-compose.yml",
        "project_name": "myapp",
        "service_name": "web",
        **overrides,
    }
    runner = MagicMock()
    runner.run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    deployer = DockerComposeDeployer(config, runner=runner)
    return deployer, runner


class TestDockerComposeDeployerInit:
    def test_defaults(self):
        deployer, _ = _make_deployer()
        assert deployer.compose_file == "docker-compose.yml"
        assert deployer.project_name == "myapp"
        assert deployer.service_name == "web"
        assert deployer.compose_command == "docker compose"


class TestComposeCmd:
    def test_builds_correct_command(self):
        deployer, _ = _make_deployer()
        cmd = deployer._compose_cmd("up", "-d")
        assert cmd == [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-p",
            "myapp",
            "up",
            "-d",
        ]


class TestExecute:
    def test_execute_success(self):
        deployer, runner = _make_deployer()
        result = deployer.execute()

        assert result.success
        assert result.status == DeploymentStatus.SUCCESS
        assert result.duration_seconds > 0

        # Should have called ps (get_current_version), pull, up, then
        # ps again (get_current_version after deploy)
        calls = runner.run.call_args_list
        cmds = [c[0][0] for c in calls]
        assert any("pull" in cmd for cmd in cmds)
        assert any("up" in cmd for cmd in cmds)

    def test_execute_failure(self):
        deployer, runner = _make_deployer()
        runner.run.side_effect = [
            subprocess.CalledProcessError(1, "docker compose pull"),
        ]

        result = deployer.execute()

        assert not result.success
        assert result.status == DeploymentStatus.FAILED

    def test_execute_pulls_specific_service(self):
        deployer, runner = _make_deployer(service_name="api")
        deployer.execute()

        cmds = [c[0][0] for c in runner.run.call_args_list]
        pull_cmds = [c for c in cmds if "pull" in c]
        assert pull_cmds
        assert "api" in pull_cmds[0]


class TestRollback:
    def test_rollback_success(self):
        deployer, _runner = _make_deployer()
        deployer._previous_tag = "v1.0.0"

        result = deployer.rollback()

        assert result.success
        assert result.status == DeploymentStatus.ROLLED_BACK

    def test_rollback_with_explicit_version(self):
        deployer, _runner = _make_deployer()

        result = deployer.rollback(to_version="v0.9.0")

        assert result.success

    def test_rollback_passes_image_tag_env(self):
        """Rollback sets IMAGE_TAG env var so compose uses the target tag."""
        deployer, runner = _make_deployer()
        deployer._previous_tag = "v1.0.0"

        result = deployer.rollback()

        assert result.success
        # The up command should have been called with env containing IMAGE_TAG
        up_calls = [
            c for c in runner.run.call_args_list if "up" in c[0][0]
        ]
        assert up_calls, "Expected a 'compose up' call during rollback"
        up_call = up_calls[0]
        env = up_call[1].get("env") or up_call.kwargs.get("env")
        assert env is not None, "Expected env kwarg with IMAGE_TAG"
        assert env.get("IMAGE_TAG") == "v1.0.0"

    def test_rollback_no_previous_tag(self):
        deployer, _runner = _make_deployer()
        deployer._previous_tag = None

        result = deployer.rollback()

        assert not result.success
        assert "No previous tag" in result.error_message


class TestHealthCheck:
    def test_healthy_when_all_running(self):
        deployer, runner = _make_deployer()
        runner.run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"Service": "web", "State": "running"}),
            stderr="",
        )

        assert deployer.health_check() is True

    def test_unhealthy_when_not_running(self):
        deployer, runner = _make_deployer()
        runner.run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"Service": "web", "State": "exited"}),
            stderr="",
        )

        assert deployer.health_check() is False

    def test_unhealthy_on_error(self):
        deployer, runner = _make_deployer()
        runner.run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error"
        )

        assert deployer.health_check() is False


class TestGetCurrentVersion:
    def test_parses_image_tag(self):
        deployer, runner = _make_deployer()
        runner.run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "Service": "web",
                    "Image": "myapp:v1.2.3",
                    "State": "running",
                }
            ),
            stderr="",
        )

        assert deployer.get_current_version() == "v1.2.3"

    def test_returns_none_when_no_service(self):
        deployer, _runner = _make_deployer(service_name=None)
        assert deployer.get_current_version() is None


class TestCliWiring:
    def test_get_deployer_returns_docker_compose(self):
        from fraisier.cli._helpers import _get_deployer

        deployer = _get_deployer(
            "docker_compose",
            {"compose_file": "dc.yml", "project_name": "test"},
        )
        assert isinstance(deployer, DockerComposeDeployer)

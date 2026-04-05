"""Tests for validate-remote command."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path  # noqa: TC003
from unittest.mock import MagicMock, patch

import yaml
from click.testing import CliRunner

from fraisier.cli.main import main


def _make_config(tmp_path: Path, config_dict: dict) -> str:
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(yaml.dump(config_dict))
    return str(config_file)


def _minimal_config() -> dict:
    return {
        "name": "myproject",
        "fraises": {
            "my_api": {
                "type": "api",
                "environments": {
                    "production": {
                        "app_path": "/var/www/app",
                    }
                },
            }
        },
        "environments": {
            "production": {
                "server": "prod.example.com",
            }
        },
    }


def _make_runner(side_effects: dict[str, str] | None = None) -> MagicMock:
    """Return a mock SSHRunner whose .run() returns stdout keyed by command."""
    runner = MagicMock()
    runner.host = "prod.example.com"
    runner.user = "fraisier"
    runner.port = 22

    side_effects = side_effects or {}

    def _run_side_effect(cmd, **kwargs):
        key = " ".join(cmd)
        if key in side_effects:
            returncode = 0 if side_effects[key] else 1
            return MagicMock(returncode=returncode, stdout=side_effects[key], stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    runner.run.side_effect = _run_side_effect
    return runner


class TestValidateRemoteArguments:
    """Test CLI argument parsing."""

    def test_missing_fraise_exits_error(self, tmp_path):
        config_file = _make_config(tmp_path, _minimal_config())
        runner = CliRunner()
        result = runner.invoke(main, ["-c", config_file, "validate-remote"])
        assert result.exit_code != 0

    def test_missing_environment_exits_error(self, tmp_path):
        config_file = _make_config(tmp_path, _minimal_config())
        runner = CliRunner()
        result = runner.invoke(main, ["-c", config_file, "validate-remote", "my_api"])
        assert result.exit_code != 0

    def test_unknown_fraise_exits_error(self, tmp_path):
        config_file = _make_config(tmp_path, _minimal_config())
        cli_runner = CliRunner()
        mock_runner = _make_runner()

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "nonexistent", "production"],
            )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_unknown_environment_exits_error(self, tmp_path):
        config_file = _make_config(tmp_path, _minimal_config())
        cli_runner = CliRunner()
        mock_runner = _make_runner()

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "nonexistent"],
            )
        assert result.exit_code == 1


class TestSSHConnectivity:
    """Test SSH connectivity check."""

    def test_ssh_failure_aborts_remaining_checks(self, tmp_path):
        config_file = _make_config(tmp_path, _minimal_config())
        cli_runner = CliRunner()
        mock_runner = MagicMock()
        mock_runner.host = "prod.example.com"
        mock_runner.user = "fraisier"
        mock_runner.port = 22
        mock_runner.run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=15)

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        assert result.exit_code == 1
        assert "ssh" in result.output.lower()

    def test_ssh_success_proceeds(self, tmp_path):
        config_file = _make_config(tmp_path, _minimal_config())
        cli_runner = CliRunner()
        mock_runner = _make_runner()

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        # Reached further than just SSH check
        assert "ssh_connectivity" in result.output


class TestGitRepoCheck:
    """Test git repo ownership check."""

    def test_git_repo_missing_fails(self, tmp_path):
        config = _minimal_config()
        config["fraises"]["my_api"]["environments"]["production"]["git_repo"] = (
            "/opt/repos/my_api.git"
        )
        config_file = _make_config(tmp_path, config)
        cli_runner = CliRunner()

        mock_runner = MagicMock()
        mock_runner.host = "prod.example.com"
        mock_runner.user = "fraisier"
        mock_runner.port = 22

        def side_effect(cmd, **kwargs):
            if cmd == ["echo", "ok"]:
                return MagicMock(returncode=0, stdout="ok", stderr="")
            if cmd[:2] == ["test", "-d"]:
                return MagicMock(returncode=1, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_runner.run.side_effect = side_effect

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        assert result.exit_code == 1
        assert "git_repo" in result.output

    def test_git_repo_wrong_owner_fails(self, tmp_path):
        config = _minimal_config()
        config["fraises"]["my_api"]["environments"]["production"]["git_repo"] = (
            "/opt/repos/my_api.git"
        )
        config_file = _make_config(tmp_path, config)
        cli_runner = CliRunner()

        mock_runner = MagicMock()
        mock_runner.host = "prod.example.com"
        mock_runner.user = "fraisier"
        mock_runner.port = 22

        def side_effect(cmd, **kwargs):
            if cmd == ["echo", "ok"]:
                return MagicMock(returncode=0, stdout="ok", stderr="")
            if cmd[:2] == ["test", "-d"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd[:2] == ["stat", "-c"]:
                return MagicMock(returncode=0, stdout="wrong_user", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_runner.run.side_effect = side_effect

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        assert result.exit_code == 1
        assert "wrong_user" in result.output

    def test_git_repo_correct_owner_passes(self, tmp_path):
        config = _minimal_config()
        config["fraises"]["my_api"]["environments"]["production"]["git_repo"] = (
            "/opt/repos/my_api.git"
        )
        config_file = _make_config(tmp_path, config)
        cli_runner = CliRunner()

        mock_runner = MagicMock()
        mock_runner.host = "prod.example.com"
        mock_runner.user = "fraisier"
        mock_runner.port = 22

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["test", "-d"] or cmd[:2] == ["test", "-f"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd[:2] == ["stat", "-c"]:
                return MagicMock(returncode=0, stdout="fraisier", stderr="")
            if "is-active" in cmd:
                return MagicMock(returncode=0, stdout="active", stderr="")
            if cmd[:2] == ["test", "-x"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_runner.run.side_effect = side_effect

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        assert "git_repo" in result.output


class TestSystemdChecks:
    """Test systemd service and socket checks."""

    def test_service_inactive_fails(self, tmp_path):
        config = _minimal_config()
        config["fraises"]["my_api"]["environments"]["production"]["systemd_service"] = (
            "my-api.service"
        )
        config_file = _make_config(tmp_path, config)
        cli_runner = CliRunner()

        mock_runner = MagicMock()
        mock_runner.host = "prod.example.com"
        mock_runner.user = "fraisier"
        mock_runner.port = 22

        def side_effect(cmd, **kwargs):
            if "is-active" in cmd:
                return MagicMock(returncode=1, stdout="inactive", stderr="")
            if cmd[:2] == ["stat", "-c"]:
                return MagicMock(returncode=0, stdout="fraisier", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_runner.run.side_effect = side_effect

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        assert result.exit_code == 1
        assert "inactive" in result.output

    def test_service_active_passes(self, tmp_path):
        config = _minimal_config()
        config["fraises"]["my_api"]["environments"]["production"]["systemd_service"] = (
            "my-api.service"
        )
        config_file = _make_config(tmp_path, config)
        cli_runner = CliRunner()

        mock_runner = MagicMock()
        mock_runner.host = "prod.example.com"
        mock_runner.user = "fraisier"
        mock_runner.port = 22

        def side_effect(cmd, **kwargs):
            if "is-active" in cmd:
                return MagicMock(returncode=0, stdout="active", stderr="")
            if cmd[:2] == ["stat", "-c"]:
                return MagicMock(returncode=0, stdout="fraisier", stderr="")
            if cmd[:2] == ["test", "-x"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_runner.run.side_effect = side_effect

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        assert "systemd_service" in result.output


class TestWrapperScripts:
    """Test wrapper script checks."""

    def test_wrapper_missing_fails(self, tmp_path):
        config_file = _make_config(tmp_path, _minimal_config())
        cli_runner = CliRunner()

        mock_runner = MagicMock()
        mock_runner.host = "prod.example.com"
        mock_runner.user = "fraisier"
        mock_runner.port = 22

        def side_effect(cmd, **kwargs):
            if cmd[:2] in (["test", "-x"], ["test", "-f"]):
                return MagicMock(returncode=1, stdout="", stderr="")
            if cmd[:2] == ["stat", "-c"]:
                return MagicMock(returncode=0, stdout="fraisier", stderr="")
            if "is-active" in cmd:
                return MagicMock(returncode=0, stdout="active", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_runner.run.side_effect = side_effect

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        assert result.exit_code == 1
        assert "wrapper" in result.output.lower()

    def test_wrapper_present_passes(self, tmp_path):
        config_file = _make_config(tmp_path, _minimal_config())
        cli_runner = CliRunner()

        mock_runner = MagicMock()
        mock_runner.host = "prod.example.com"
        mock_runner.user = "fraisier"
        mock_runner.port = 22

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["test", "-x"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd[:2] == ["stat", "-c"]:
                return MagicMock(returncode=0, stdout="fraisier", stderr="")
            if "is-active" in cmd:
                return MagicMock(returncode=0, stdout="active", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_runner.run.side_effect = side_effect

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        assert "systemctl_wrapper" in result.output
        assert "pg_wrapper" in result.output


class TestSudoers:
    """Test sudoers fragment check."""

    def test_sudoers_missing_is_warning_not_error(self, tmp_path):
        config_file = _make_config(tmp_path, _minimal_config())
        cli_runner = CliRunner()

        mock_runner = MagicMock()
        mock_runner.host = "prod.example.com"
        mock_runner.user = "fraisier"
        mock_runner.port = 22

        def side_effect(cmd, **kwargs):
            if cmd[:2] == ["test", "-f"] and "sudoers" in " ".join(cmd):
                return MagicMock(returncode=1, stdout="", stderr="")
            if cmd[:2] == ["test", "-x"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd[:2] == ["stat", "-c"]:
                return MagicMock(returncode=0, stdout="fraisier", stderr="")
            if "is-active" in cmd:
                return MagicMock(returncode=0, stdout="active", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_runner.run.side_effect = side_effect

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        # Sudoers missing is a warning, not an error — exit 0 expected
        assert result.exit_code == 0
        assert "sudoers" in result.output.lower()


class TestHealthEndpoint:
    """Test health endpoint check."""

    def test_health_check_skipped_when_not_configured(self, tmp_path):
        config_file = _make_config(tmp_path, _minimal_config())
        cli_runner = CliRunner()
        mock_runner = _make_runner()

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        assert "skipped" in result.output.lower()

    def test_health_check_failure_fails(self, tmp_path):
        import urllib.error

        config = _minimal_config()
        config["fraises"]["my_api"]["environments"]["production"]["health_check"] = {
            "url": "http://prod.example.com/health",
        }
        config_file = _make_config(tmp_path, config)
        cli_runner = CliRunner()
        mock_runner = _make_runner()

        with (
            patch(
                "fraisier.cli.validate_remote._resolve_server_and_runner"
            ) as mock_resolve,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        assert result.exit_code == 1
        assert "health_endpoint" in result.output

    def test_health_check_passes(self, tmp_path):
        config = _minimal_config()
        config["fraises"]["my_api"]["environments"]["production"]["health_check"] = {
            "url": "http://prod.example.com/health",
        }
        config_file = _make_config(tmp_path, config)
        cli_runner = CliRunner()
        mock_runner = _make_runner()

        with (
            patch(
                "fraisier.cli.validate_remote._resolve_server_and_runner"
            ) as mock_resolve,
            patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            mock_response = MagicMock()
            mock_response.status = 200
            mock_urlopen.return_value = mock_response
            result = cli_runner.invoke(
                main,
                ["-c", config_file, "validate-remote", "my_api", "production"],
            )
        assert "health_endpoint" in result.output


class TestJSONOutput:
    """Test JSON output format."""

    def test_json_output_structure(self, tmp_path):
        config_file = _make_config(tmp_path, _minimal_config())
        cli_runner = CliRunner()
        mock_runner = _make_runner()

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                [
                    "-c",
                    config_file,
                    "validate-remote",
                    "my_api",
                    "production",
                    "--json",
                ],
            )

        data = json.loads(result.output)
        assert "passed" in data
        assert "checks" in data
        assert "server" in data
        assert "fraise" in data
        assert "environment" in data
        assert isinstance(data["checks"], list)

    def test_json_check_structure(self, tmp_path):
        config_file = _make_config(tmp_path, _minimal_config())
        cli_runner = CliRunner()
        mock_runner = _make_runner()

        with patch(
            "fraisier.cli.validate_remote._resolve_server_and_runner"
        ) as mock_resolve:
            mock_resolve.return_value = ("prod.example.com", mock_runner)
            result = cli_runner.invoke(
                main,
                [
                    "-c",
                    config_file,
                    "validate-remote",
                    "my_api",
                    "production",
                    "--json",
                ],
            )

        data = json.loads(result.output)
        for check in data["checks"]:
            assert "name" in check
            assert "passed" in check

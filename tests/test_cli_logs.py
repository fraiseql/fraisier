"""Tests for logs command."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from fraisier.cli.main import main


class TestLogsCommand:
    """Test logs command functionality."""

    def test_resolve_deploy_unit_pattern(self):
        """Test unit pattern construction from config."""
        # Mock config
        config = MagicMock()
        config.project_name = "myproject"

        from fraisier.cli.logs import _resolve_deploy_unit_pattern

        pattern = _resolve_deploy_unit_pattern(config, "api", "production")
        expected = "fraisier-myproject-api-production-deploy@*.service"
        assert pattern == expected

    @patch("fraisier.cli.logs.os.execvp")
    def test_logs_command_builds_journalctl_args_follow(self, mock_execvp):
        """Logs command builds correct journalctl args for follow mode."""
        runner = CliRunner()

        # Mock config
        config = MagicMock()
        config.project_name = "myproject"
        config.get_fraise_environment.return_value = {"type": "api"}

        with patch("fraisier.cli.main.get_config", return_value=config):
            runner.invoke(
                main,
                ["logs", "api", "production"],
                obj={"config": config, "skip_health": False},
            )

            assert mock_execvp.called
            args, _kwargs = mock_execvp.call_args
            assert args[0] == "journalctl"
            assert "-u" in args[1]
            assert "fraisier-myproject-api-production-deploy@*.service" in args[1]
            assert "-f" in args[1]  # follow mode

    @patch("fraisier.cli.logs.os.execvp")
    def test_logs_command_builds_journalctl_args_no_follow(self, mock_execvp):
        """Logs command builds correct journalctl args for no-follow mode."""
        runner = CliRunner()

        # Mock config
        config = MagicMock()
        config.project_name = "myproject"
        config.get_fraise_environment.return_value = {"type": "api"}

        with patch("fraisier.cli.main.get_config", return_value=config):
            runner.invoke(
                main,
                ["logs", "api", "production", "--no-follow", "--lines", "100"],
                obj={"config": config, "skip_health": False},
            )

            assert mock_execvp.called
            args, _kwargs = mock_execvp.call_args
            assert args[0] == "journalctl"
            assert "-u" in args[1]
            assert "fraisier-myproject-api-production-deploy@*.service" in args[1]
            assert "-f" not in args[1]  # no follow
            assert "-n" in args[1]
            assert "100" in args[1]

    def test_logs_invalid_fraise_shows_error(self):
        """Logs command shows error for invalid fraise/environment."""
        runner = CliRunner()

        # Mock config
        config = MagicMock()
        config.get_fraise_environment.return_value = None

        with patch("fraisier.cli.main.get_config", return_value=config):
            result = runner.invoke(
                main,
                ["logs", "invalid", "fraise"],
                obj={"config": config, "skip_health": False},
            )

            assert result.exit_code == 1
            assert "not found" in result.output

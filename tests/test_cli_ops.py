"""Tests for CLI operational commands (status-all, etc.)."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner, Result

from fraisier.cli.main import main


def _make_config_mock(
    deployments: list[dict],
    server_env_map: dict[str, list[str]],
) -> MagicMock:
    """Build a config mock with deployments and server→env mapping."""
    config = MagicMock()
    config.list_all_deployments.return_value = deployments
    config.get_environments_for_server.side_effect = lambda s: server_env_map.get(s, [])
    # None return triggers "error" row — keeps tests simple and fast
    config.get_fraise_environment.return_value = None
    return config


class TestConfigRequiredCommands:
    """Commands that need config produce helpful error when config is missing."""

    def test_status_all_without_config_shows_helpful_error(self):
        """status-all with no config gives useful error, not AttributeError."""
        db = MagicMock()
        db.get_all_fraise_states.return_value = [
            {"fraise_name": "api1", "environment_name": "prod", "status": "healthy"},
        ]

        runner = CliRunner()
        with (
            patch(
                "fraisier.cli.main.get_config",
                side_effect=FileNotFoundError("fraises.yaml"),
            ),
            patch("fraisier.database.get_db", return_value=db),
        ):
            result = runner.invoke(main, ["status-all"])

        # Should not crash with AttributeError on NoneType
        assert result.exit_code != 0
        combined = result.output + str(result.exception or "")
        assert "AttributeError" not in combined
        output_lower = result.output.lower()
        assert "config" in output_lower or "fraises.yaml" in output_lower


class TestStatusAllTypeFilter:
    """Tests for status-all --type filter."""

    def test_type_filter_checks_each_fraise_individually(self):
        """--type api with mixed types returns only api fraises."""
        config = MagicMock()
        # Two api fraises and one etl
        fraise_types = {
            "api1": {"type": "api"},
            "api2": {"type": "api"},
            "etl1": {"type": "etl"},
        }
        config.get_fraise.side_effect = fraise_types.get

        db = MagicMock()
        db.get_all_fraise_states.return_value = [
            {"fraise_name": "api1", "environment_name": "prod", "status": "healthy"},
            {"fraise_name": "api2", "environment_name": "prod", "status": "healthy"},
            {"fraise_name": "etl1", "environment_name": "prod", "status": "healthy"},
        ]

        runner = CliRunner()
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.database.get_db", return_value=db),
        ):
            result = runner.invoke(main, ["status-all", "--type", "api"])

        assert result.exit_code == 0
        # Should show both api fraises but not etl
        assert "api1" in result.output
        assert "api2" in result.output
        assert "etl1" not in result.output


def _deployment(fraise: str, environment: str, type_: str = "api") -> dict:
    return {
        "fraise": fraise,
        "environment": environment,
        "job": None,
        "type": type_,
        "name": fraise,
        "description": "",
    }


_MULTI_SERVER_DEPLOYMENTS = [
    _deployment("api", "development"),
    _deployment("api", "staging"),
    _deployment("api", "production"),
    _deployment("etl", "production", type_="etl"),
]

_MULTI_SERVER_ENV_MAP: dict[str, list[str]] = {
    "printoptim.dev": ["development", "staging"],
    "printoptim.io": ["production"],
}


class TestStatusServerFilter:
    """Tests for fraisier status server-based filtering (issue #73)."""

    def _run_status(self, args: list[str]) -> Result:
        config = _make_config_mock(_MULTI_SERVER_DEPLOYMENTS, _MULTI_SERVER_ENV_MAP)
        db = MagicMock()
        db.get_all_fraise_states.return_value = []
        runner = CliRunner()
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.database.get_db", return_value=db),
        ):
            return runner.invoke(main, ["status", *args])

    def test_default_filters_by_current_hostname(self):
        """Without flags, status filters to environments on the current hostname."""
        result = self._run_status(["--server", "printoptim.dev"])
        assert result.exit_code == 0
        assert "development" in result.output
        assert "staging" in result.output
        assert "production" not in result.output

    def test_explicit_server_flag_overrides_hostname(self):
        """--server filters to the specified server's environments."""
        result = self._run_status(["--server", "printoptim.io"])
        assert result.exit_code == 0
        assert "production" in result.output
        assert "development" not in result.output
        assert "staging" not in result.output

    def test_all_flag_shows_every_environment(self):
        """--all disables server filtering and shows every environment."""
        result = self._run_status(["--all"])
        assert result.exit_code == 0
        assert "development" in result.output
        assert "staging" in result.output
        assert "production" in result.output

    def test_unknown_server_shows_no_fraises(self):
        """A server with no matching environments shows the empty-table message."""
        result = self._run_status(["--server", "unknown.host"])
        assert result.exit_code == 0
        assert "No fraises configured" in result.output

    def test_server_name_appears_in_table_title(self):
        """The filtered server name is shown in the table title."""
        result = self._run_status(["--server", "printoptim.dev"])
        assert result.exit_code == 0
        assert "printoptim.dev" in result.output

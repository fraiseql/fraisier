"""Tests for CLI operational commands (status-all, etc.)."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from fraisier.cli.main import main


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

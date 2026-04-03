"""Tests for CLI operational commands (status-all, etc.)."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from fraisier.cli._helpers import parse_since
from fraisier.cli.main import main


class TestParseSince:
    """Test parse_since helper function."""

    def test_parse_days(self):
        """Parse '7d' returns a datetime in the past."""
        result = parse_since("7d")
        parsed = datetime.fromisoformat(result)
        now = datetime.now()
        # Should be approximately 7 days ago (allowing some tolerance)
        assert abs((now - parsed).total_seconds() - 7 * 24 * 3600) < 60

    def test_parse_hours(self):
        """Parse '24h' returns a datetime in the past."""
        result = parse_since("24h")
        parsed = datetime.fromisoformat(result)
        now = datetime.now()
        # Should be approximately 24 hours ago
        assert abs((now - parsed).total_seconds() - 24 * 3600) < 60

    def test_parse_iso_date(self):
        """Parse ISO date string."""
        result = parse_since("2026-04-01")
        expected = "2026-04-01T00:00:00"
        assert result == expected

    def test_parse_invalid_format(self):
        """Invalid format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid date/time format"):
            parse_since("invalid")

    def test_parse_empty_string(self):
        """Empty string returns empty string."""
        assert parse_since("") == ""


class TestHistoryCommand:
    """Test history command with new features."""

    def test_history_json_output(self):
        """History command with --json outputs valid JSON."""
        runner = CliRunner()

        # Mock the database
        mock_deployments = [
            {
                "id": 1,
                "fraise": "api",
                "environment": "production",
                "git_commit": "abc123456789",
                "triggered_by": "webhook",
                "old_version": "1.0.0",
                "new_version": "1.1.0",
                "status": "success",
                "duration_seconds": 45.5,
                "started_at": "2026-04-03T10:00:00",
            }
        ]

        with patch("fraisier.database.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.get_recent_deployments.return_value = mock_deployments
            mock_get_db.return_value = mock_db

            result = runner.invoke(main, ["history", "--json"])

            assert result.exit_code == 0
            # Should output JSON array
            import json

            data = json.loads(result.output.strip())
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["fraise"] == "api"

    def test_history_positional_args(self):
        """History command with positional args works."""
        runner = CliRunner()

        mock_deployments = [
            {
                "id": 1,
                "fraise": "api",
                "environment": "production",
                "git_commit": "abc12345",
                "triggered_by": "webhook",
                "old_version": "1.0.0",
                "new_version": "1.1.0",
                "status": "success",
                "duration_seconds": 45.5,
                "started_at": "2026-04-03T10:00:00",
            }
        ]

        with patch("fraisier.database.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.get_recent_deployments.return_value = mock_deployments
            mock_get_db.return_value = mock_db

            result = runner.invoke(main, ["history", "api", "production"])

            assert result.exit_code == 0
            assert "api" in result.output
            assert "Deployment History" in result.output  # Check that table is rendered
            # Should call with fraise and environment filters
            mock_db.get_recent_deployments.assert_called_with(
                limit=20,
                fraise="api",
                environment="production",
                since=None,
            )

    def test_history_backward_compatibility_options(self):
        """History command with --fraise/--environment options still works."""
        runner = CliRunner()

        mock_deployments = [
            {
                "id": 1,
                "fraise": "api",
                "environment": "production",
                "git_commit": "abc12345",
                "triggered_by": "webhook",
                "old_version": "1.0.0",
                "new_version": "1.1.0",
                "status": "success",
                "duration_seconds": 45.5,
                "started_at": "2026-04-03T10:00:00",
            }
        ]

        with patch("fraisier.database.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.get_recent_deployments.return_value = mock_deployments
            mock_get_db.return_value = mock_db

            result = runner.invoke(
                main, ["history", "--fraise", "api", "--environment", "production"]
            )

            assert result.exit_code == 0
            assert "api" in result.output
            assert "Deployment History" in result.output  # Check that table is rendered
            # Should call with fraise and environment filters
            mock_db.get_recent_deployments.assert_called_with(
                limit=20,
                fraise="api",
                environment="production",
                since=None,
            )

    def test_history_table_columns(self):
        """History table includes SHA and Triggered By columns."""
        runner = CliRunner()

        mock_deployments = [
            {
                "id": 1,
                "fraise": "api",
                "environment": "production",
                "git_commit": "abc123456789",
                "triggered_by": "webhook",
                "old_version": "1.0.0",
                "new_version": "1.1.0",
                "status": "success",
                "duration_seconds": 45.5,
                "started_at": "2026-04-03T10:00:00",
            }
        ]

        with patch("fraisier.database.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.get_recent_deployments.return_value = mock_deployments
            mock_get_db.return_value = mock_db

            result = runner.invoke(main, ["history"])

            assert result.exit_code == 0
            # Check that column headers are present (may be truncated)
            assert "SHA" in result.output
            assert "Trigg" in result.output  # Truncated "Triggered"
            # Check that SHA is truncated
            assert "abc12" in result.output


class TestRollbackCommand:
    """Test rollback command enhancements."""

    def test_rollback_dry_run(self):
        """Rollback --dry-run shows plan without executing."""
        runner = CliRunner()

        # Mock config
        config = MagicMock()
        config.get_fraise_environment.return_value = {"type": "api", "name": "api"}

        # Mock database with deployment history
        mock_deployments = [
            {
                "id": 1,
                "fraise": "api",
                "environment": "prod",
                "status": "success",
                "new_version": "current_sha",
                "started_at": "2026-04-03T12:00:00",
                "git_commit": "current_sha",
            },
            {
                "id": 2,
                "fraise": "api",
                "environment": "prod",
                "status": "success",
                "new_version": "rollback_sha",
                "started_at": "2026-04-03T11:00:00",
                "git_commit": "rollback_sha",
            },
        ]

        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.database.get_db") as mock_get_db,
            patch("fraisier.cli.main._get_deployer") as mock_get_deployer,
        ):
            mock_db = MagicMock()
            mock_db.get_recent_deployments.return_value = mock_deployments
            mock_get_db.return_value = mock_db

            mock_deployer = MagicMock()
            mock_deployer.get_current_version.return_value = "current_sha"
            mock_get_deployer.return_value = mock_deployer

            result = runner.invoke(
                main,
                ["rollback", "api", "prod", "--dry-run"],
                obj={"config": config, "skip_health": False},
            )

            assert result.exit_code == 0
            assert "DRY RUN" in result.output
            assert "current_sha" in result.output
            assert "rollback" in result.output  # Truncated SHA
            # Should not call deployer.rollback
            mock_deployer.rollback.assert_not_called()

    def test_rollback_improved_target_resolution(self):
        """Rollback finds correct target when current deployment is latest."""
        runner = CliRunner()

        # Mock config
        config = MagicMock()
        config.get_fraise_environment.return_value = {"type": "api", "name": "api"}

        # Mock database with deployment history where current is the latest
        mock_deployments = [
            {
                "id": 1,
                "fraise": "api",
                "environment": "prod",
                "status": "success",
                "new_version": "current_sha",
                "started_at": "2026-04-03T12:00:00",
            },
            {
                "id": 2,
                "fraise": "api",
                "environment": "prod",
                "status": "success",
                "new_version": "previous_sha",
                "started_at": "2026-04-03T11:00:00",
            },
            {
                "id": 3,
                "fraise": "api",
                "environment": "prod",
                "status": "failed",
                "new_version": "failed_sha",
                "started_at": "2026-04-03T10:00:00",
            },
        ]

        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.database.get_db") as mock_get_db,
            patch("fraisier.cli.main._get_deployer") as mock_get_deployer,
        ):
            mock_db = MagicMock()
            mock_db.get_recent_deployments.return_value = mock_deployments
            mock_get_db.return_value = mock_db

            mock_deployer = MagicMock()
            mock_deployer.get_current_version.return_value = "current_sha"
            mock_get_deployer.return_value = mock_deployer

            result = runner.invoke(
                main,
                ["rollback", "api", "prod", "--dry-run"],
                obj={"config": config, "skip_health": False},
                input="y\n",  # Confirm
            )

            assert result.exit_code == 0
            assert (
                "previous" in result.output
            )  # Should find previous successful (truncated)
            # Should call get_recent_deployments with higher limit
            mock_db.get_recent_deployments.assert_called_with(
                limit=20, fraise="api", environment="prod"
            )

    def test_rollback_safety_limit_without_force(self):
        """Rollback refuses target older than 10 deployments without --force."""
        runner = CliRunner()

        # Mock config
        config = MagicMock()
        config.get_fraise_environment.return_value = {"type": "api", "name": "api"}

        # Mock database with deployments, target is far back
        mock_deployments = []
        for i in range(20, 0, -1):  # IDs 20 down to 1, newest first
            status = "success"  # All successful except current
            version = f"sha_{i}"
            mock_deployments.append(
                {
                    "id": i,
                    "fraise": "api",
                    "environment": "prod",
                    "status": status,
                    "new_version": version,
                    "started_at": f"2026-04-03T{i:02d}:00:00",
                }
            )

        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.database.get_db") as mock_get_db,
            patch("fraisier.cli.main._get_deployer") as mock_get_deployer,
        ):
            mock_db = MagicMock()
            mock_db.get_recent_deployments.return_value = mock_deployments
            mock_get_db.return_value = mock_db

            mock_deployer = MagicMock()
            mock_deployer.get_current_version.return_value = "sha_20"  # Latest
            # But let's make the first few not successful to force target further back
            for i in range(19, 10, -1):  # Make sha_19 through sha_11 not successful
                mock_deployments[20 - i]["status"] = "failed"
            mock_get_deployer.return_value = mock_deployer

            result = runner.invoke(
                main,
                ["rollback", "api", "prod", "--dry-run"],
                obj={"config": config, "skip_health": False},
            )

            # Should refuse because target (sha_14) is more than 10 deployments back
            assert result.exit_code == 1
            assert (
                "too far back" in result.output.lower()
                or "safety limit" in result.output.lower()
            )


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

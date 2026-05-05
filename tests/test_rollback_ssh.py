"""Tests for rollback SSH dispatch (issue #194, Phase 2)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.cli.ops import RemoteHistoryError

REMOTE_HISTORY = [
    {
        "id": 99,
        "fraise": "api",
        "environment": "production",
        "status": "success",
        "new_version": "1.3.0",
        "started_at": "2026-05-04T12:00:00",
    },
    {
        "id": 88,
        "fraise": "api",
        "environment": "production",
        "status": "success",
        "new_version": "1.2.9",
        "started_at": "2026-05-03T10:00:00",
    },
]


def _config_with_ssh():
    config = MagicMock()
    config.get_fraise_environment.return_value = {
        "type": "api",
        "ssh": {
            "host": "prod.example.com",
            "user": "deploy",
            "db_path": "/var/lib/fraisier/fraisier.db",
            "fraisier_bin": "/home/deploy/.local/bin/fraisier",
        },
    }
    return config


def _config_no_ssh():
    config = MagicMock()
    config.get_fraise_environment.return_value = {"type": "api"}
    return config


def _mock_deployer(current_version="1.3.0"):
    deployer = MagicMock()
    deployer.get_current_version.return_value = current_version
    deployer.rollback.return_value = MagicMock(success=True, new_version="1.2.9")
    return deployer


class TestRollbackSshDispatch:
    def test_resolves_version_from_remote(self):
        """Without --to-version, rollback fetches history from remote."""
        runner = CliRunner()
        config = _config_with_ssh()
        deployer = _mock_deployer()
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli._rollback._get_deployer", return_value=deployer),
            patch(
                "fraisier.cli._rollback._remote_history_fetch",
                return_value=REMOTE_HISTORY,
            ) as mock_fetch,
        ):
            result = runner.invoke(main, ["rollback", "api", "production", "--force"])
        assert result.exit_code == 0, result.output
        mock_fetch.assert_called_once()
        deployer.rollback.assert_called_once_with(to_version="1.2.9")
        # Pre-action notice with host
        assert "prod.example.com" in result.output
        assert "Fetching history" in result.output

    def test_with_to_version_skips_remote_fetch(self):
        """--to-version bypasses history lookup entirely."""
        runner = CliRunner()
        config = _config_with_ssh()
        deployer = _mock_deployer()
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli._rollback._get_deployer", return_value=deployer),
            patch("fraisier.cli._rollback._remote_history_fetch") as mock_fetch,
        ):
            result = runner.invoke(
                main,
                ["rollback", "api", "production", "--to-version", "1.2.8", "--force"],
            )
        assert result.exit_code == 0, result.output
        mock_fetch.assert_not_called()
        deployer.rollback.assert_called_once_with(to_version="1.2.8")

    def test_no_ssh_uses_local_db(self):
        """Without SSH config, local DB lookup is unchanged."""
        runner = CliRunner()
        config = _config_no_ssh()
        deployer = _mock_deployer()
        mock_db = MagicMock()
        mock_db.get_recent_deployments.return_value = REMOTE_HISTORY
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli._rollback._get_deployer", return_value=deployer),
            patch("fraisier.database.get_db", return_value=mock_db),
            patch("fraisier.cli._rollback._remote_history_fetch") as mock_fetch,
        ):
            result = runner.invoke(main, ["rollback", "api", "production", "--force"])
        assert result.exit_code == 0, result.output
        mock_fetch.assert_not_called()
        mock_db.get_recent_deployments.assert_called_once()

    def test_empty_remote_history_aborts(self):
        """Empty remote history aborts with a clear error."""
        runner = CliRunner()
        config = _config_with_ssh()
        deployer = _mock_deployer()
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli._rollback._get_deployer", return_value=deployer),
            patch("fraisier.cli._rollback._remote_history_fetch", return_value=[]),
        ):
            result = runner.invoke(main, ["rollback", "api", "production"])
        assert result.exit_code == 1

    def test_ssh_connection_error_shows_hint(self):
        """SSH failure exits 1 with Error + Hint."""
        runner = CliRunner()
        config = _config_with_ssh()
        deployer = _mock_deployer()
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli._rollback._get_deployer", return_value=deployer),
            patch(
                "fraisier.cli._rollback._remote_history_fetch",
                side_effect=RemoteHistoryError("Connection refused"),
            ),
        ):
            result = runner.invoke(main, ["rollback", "api", "production"])
        assert result.exit_code == 1
        assert "Connection refused" in result.output
        assert "ssh prod.example.com echo ok" in result.output

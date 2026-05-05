"""Tests for stats SSH dispatch and webhooks notice (issue #194, Phase 3)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from fraisier.cli.main import main

REMOTE_STATS = {
    "total": 150,
    "successful": 140,
    "failed": 8,
    "rolled_back": 2,
    "avg_duration": 35.2,
}


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


class TestStatsSshDispatch:
    def test_ssh_dispatch_with_env(self):
        """stats --fraise api --env production SSHes when SSH-configured."""
        runner = CliRunner()
        config = _config_with_ssh()
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli.ops.ssh.short_cmd") as mock_ssh,
        ):
            mock_ssh.return_value = MagicMock(stdout=json.dumps(REMOTE_STATS))
            result = runner.invoke(
                main, ["stats", "--fraise", "api", "--env", "production"]
            )
        assert result.exit_code == 0, result.output
        mock_ssh.assert_called_once()
        assert "150" in result.output
        # Pre-action notice with host
        assert "prod.example.com" in result.output
        assert "Fetching stats" in result.output

    def test_no_ssh_uses_local_db(self):
        """stats without SSH config reads local DB."""
        runner = CliRunner()
        config = _config_no_ssh()
        mock_db = MagicMock()
        mock_db.get_deployment_stats.return_value = REMOTE_STATS
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.database.get_db", return_value=mock_db),
            patch("fraisier.cli.ops.ssh.short_cmd") as mock_ssh,
        ):
            result = runner.invoke(
                main, ["stats", "--fraise", "api", "--env", "staging"]
            )
        assert result.exit_code == 0, result.output
        mock_ssh.assert_not_called()

    def test_no_env_uses_local_db(self):
        """stats without --env always uses local DB."""
        runner = CliRunner()
        mock_db = MagicMock()
        mock_db.get_deployment_stats.return_value = REMOTE_STATS
        with (
            patch("fraisier.database.get_db", return_value=mock_db),
            patch("fraisier.cli.ops.ssh.short_cmd") as mock_ssh,
        ):
            result = runner.invoke(main, ["stats", "--fraise", "api"])
        assert result.exit_code == 0
        mock_ssh.assert_not_called()


class TestWebhooksNotice:
    def test_webhooks_shows_local_notice(self):
        """webhooks output includes a local-only notice."""
        runner = CliRunner()
        mock_db = MagicMock()
        mock_db.get_recent_webhooks.return_value = []
        with patch("fraisier.database.get_db", return_value=mock_db):
            result = runner.invoke(main, ["webhooks"])
        assert result.exit_code == 0
        assert "local" in result.output.lower()

"""Tests for SSH-dispatched history helpers (issue #194)."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from fraisier.cli.main import main

FAKE_DEPLOYMENTS = [
    {
        "id": 1,
        "fraise": "api",
        "environment": "production",
        "git_commit": "abc1234",
        "triggered_by": "webhook",
        "new_version": "1.2.3",
        "status": "success",
        "duration_seconds": 42.0,
        "started_at": "2026-05-01T10:00:00",
        "old_version": "1.2.2",
        "deployment_type": "upgrade",
    }
]


def _config_with_ssh():
    """Build a config mock where api/production has SSH config."""
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
    """Build a config mock where the environment has no SSH block."""
    config = MagicMock()
    config.get_fraise_environment.return_value = {"type": "api"}
    return config


class TestBuildRemoteHistoryArgv:
    def test_with_db_path(self):
        from fraisier.cli.ops import _build_remote_history_argv

        argv = _build_remote_history_argv(
            fraise="api",
            environment="production",
            limit=20,
            since=None,
            fraisier_bin="/home/deploy/.local/bin/fraisier",
            db_path="/var/lib/fraisier/fraisier.db",
        )
        assert argv[0] == "sh"
        assert argv[1] == "-c"
        assert "FRAISIER_DB_PATH=/var/lib/fraisier/fraisier.db" in argv[2]
        assert "--json" in argv[2]
        assert "--limit 20" in argv[2]

    def test_without_db_path(self):
        from fraisier.cli.ops import _build_remote_history_argv

        argv = _build_remote_history_argv(
            fraise="api",
            environment="production",
            limit=5,
            since="2026-04-01T00:00:00",
            fraisier_bin="fraisier",
            db_path=None,
        )
        assert "FRAISIER_DB_PATH" not in argv[2]
        assert "--since 2026-04-01T00:00:00" in argv[2]
        assert "--limit 5" in argv[2]

    def test_quotes_special_chars_in_db_path(self):
        from fraisier.cli.ops import _build_remote_history_argv

        argv = _build_remote_history_argv(
            fraise="api",
            environment="production",
            limit=10,
            since=None,
            fraisier_bin="fraisier",
            db_path="/var lib/fraisier/fraisier.db",
        )
        # shlex.quote must have escaped the space
        assert "'/var lib/fraisier/fraisier.db'" in argv[2]


class TestHistorySshDispatch:
    def test_ssh_dispatch_when_env_has_ssh(self):
        """When environment has SSH config, history SSHes instead of reading local DB."""
        runner = CliRunner()
        config = _config_with_ssh()
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli.ops.ssh.short_cmd") as mock_ssh,
        ):
            mock_ssh.return_value = MagicMock(
                stdout=json.dumps(FAKE_DEPLOYMENTS),
                returncode=0,
            )
            result = runner.invoke(main, ["history", "api", "production"])
        assert result.exit_code == 0, result.output
        mock_ssh.assert_called_once()
        remote_argv = mock_ssh.call_args[0][1]
        assert remote_argv[0] == "sh"
        # Pre-action notice with host
        assert "prod.example.com" in result.output
        assert "Fetching history" in result.output

    def test_no_ssh_uses_local_db(self):
        """When environment has no SSH config, history reads local DB."""
        runner = CliRunner()
        config = _config_no_ssh()
        mock_db = MagicMock()
        mock_db.get_recent_deployments.return_value = FAKE_DEPLOYMENTS
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.database.get_db", return_value=mock_db),
            patch("fraisier.cli.ops.ssh.short_cmd") as mock_ssh,
        ):
            result = runner.invoke(main, ["history", "api", "production"])
        assert result.exit_code == 0, result.output
        mock_ssh.assert_not_called()

    def test_no_fraise_arg_uses_local_db(self):
        """history without fraise/env args always uses local DB."""
        runner = CliRunner()
        mock_db = MagicMock()
        mock_db.get_recent_deployments.return_value = []
        with (
            patch("fraisier.database.get_db", return_value=mock_db),
            patch("fraisier.cli.ops.ssh.short_cmd") as mock_ssh,
        ):
            result = runner.invoke(main, ["history"])
        assert result.exit_code == 0
        mock_ssh.assert_not_called()

    def test_ssh_json_flag_redumps(self):
        """--json flag on local CLI re-dumps the parsed remote JSON."""
        runner = CliRunner()
        config = _config_with_ssh()
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli.ops.ssh.short_cmd") as mock_ssh,
        ):
            mock_ssh.return_value = MagicMock(
                stdout=json.dumps(FAKE_DEPLOYMENTS),
            )
            result = runner.invoke(main, ["history", "api", "production", "--json"])
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert len(parsed) == 1
        assert parsed[0]["fraise"] == "api"


class TestHistorySshErrorHandling:
    def test_empty_result(self):
        """Remote returns empty list — renders 'no deployments' message."""
        runner = CliRunner()
        config = _config_with_ssh()
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli.ops.ssh.short_cmd") as mock_ssh,
        ):
            mock_ssh.return_value = MagicMock(stdout="[]")
            result = runner.invoke(main, ["history", "api", "production"])
        assert result.exit_code == 0
        assert "No deployments" in result.output

    def test_connection_error_shows_hint(self):
        """SSH connection failure exits 1 with Error + Hint."""
        runner = CliRunner()
        config = _config_with_ssh()
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli.ops.ssh.short_cmd") as mock_ssh,
        ):
            mock_ssh.side_effect = subprocess.CalledProcessError(
                255, "ssh", stderr="Connection refused"
            )
            result = runner.invoke(main, ["history", "api", "production"])
        assert result.exit_code == 1
        assert "Connection refused" in result.output
        assert "ssh prod.example.com echo ok" in result.output

    def test_invalid_json_shows_error(self):
        """Malformed JSON from remote exits 1 with Error prefix."""
        runner = CliRunner()
        config = _config_with_ssh()
        with (
            patch("fraisier.cli.main.get_config", return_value=config),
            patch("fraisier.cli.ops.ssh.short_cmd") as mock_ssh,
        ):
            mock_ssh.return_value = MagicMock(stdout="not json")
            result = runner.invoke(main, ["history", "api", "production"])
        assert result.exit_code == 1
        assert "malformed JSON" in result.output

"""Tests for trigger-deploy command with --wait and --follow."""

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from fraisier.cli.main import main
from fraisier.duration_estimate import EstimateResult


class TestTriggerDeployWait:
    """Test trigger-deploy --wait functionality."""

    def test_deploy_daemon_writes_json_result(self):
        """Test deploy_daemon command writes JSON result to stdout."""
        runner = CliRunner()

        # Mock deployment request JSON
        request_json = json.dumps(
            {
                "version": 1,
                "project": "test_project",
                "environment": "production",
                "branch": "main",
                "timestamp": "2026-04-03T10:00:00Z",
                "triggered_by": "cli",
                "options": {"force": False, "no_cache": False, "dry_run": False},
                "metadata": {"cli_user": "testuser"},
            }
        )

        with patch("fraisier.daemon.execute_deployment_request") as mock_execute:
            # Mock successful deployment result
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.status = "success"
            mock_result.deployed_version = "abc123"
            mock_result.duration_seconds = 45.5
            mock_result.error_message = None
            mock_result.message = "Deployment completed"
            mock_execute.return_value = mock_result

            # Run deploy-daemon with stdin input
            result = runner.invoke(
                main, ["deploy-daemon", "--project", "test_project"], input=request_json
            )

            assert result.exit_code == 0

            # Check that JSON was written to stdout
            output_lines = result.output.strip().split("\n")
            json_line = None
            for line in output_lines:
                if line.strip().startswith("{"):
                    json_line = line
                    break

            assert json_line is not None, f"No JSON found in output: {result.output}"
            parsed = json.loads(json_line)
            assert parsed["success"] is True
            assert parsed["version"] == "abc123"
            assert parsed["status"] == "success"

    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_trigger_deploy_basic(
        self, mock_get_config, mock_path_class, mock_socket_class
    ):
        """Test basic trigger-deploy without wait works."""
        runner = CliRunner()

        # Mock config
        config = MagicMock()
        config.project_name = "myproject"
        config.get_fraise_environment.return_value = {"type": "api"}
        mock_get_config.return_value = config

        # Mock Path
        mock_path = MagicMock()
        mock_path_class.return_value = mock_path
        mock_socket_path = MagicMock()
        mock_path.__truediv__.return_value = mock_socket_path
        mock_socket_path.__str__.return_value = (
            "/run/fraisier/myproject-prod/deploy.sock"
        )

        # Mock socket
        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock
        mock_sock.connect.return_value = None  # Mock successful connect
        mock_sock.sendall.return_value = None  # Mock successful send
        mock_sock.shutdown.return_value = None  # Mock successful shutdown

        result = runner.invoke(
            main,
            ["trigger-deploy", "api", "prod"],
            obj={"skip_health": False},
        )

        # Should succeed with basic message
        assert result.exit_code == 0
        assert "Deployment triggered successfully" in result.output

    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_trigger_deploy_wait_reads_response(
        self, mock_get_config, mock_path_class, mock_socket_class
    ):
        """Test trigger-deploy --wait reads and parses socket response."""
        runner = CliRunner()

        # Mock config
        config = MagicMock()
        config.project_name = "myproject"
        config.get_fraise_environment.return_value = {"type": "api"}
        mock_get_config.return_value = config

        # Mock Path
        mock_path = MagicMock()
        mock_path_class.return_value = mock_path
        mock_socket_path = MagicMock()
        mock_path.__truediv__.return_value = mock_socket_path
        mock_socket_path.__str__.return_value = (
            "/run/fraisier/myproject-prod/deploy.sock"
        )

        # Mock socket
        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock
        mock_sock.connect.return_value = None  # Mock successful connect
        mock_sock.sendall.return_value = None  # Mock successful send
        mock_sock.shutdown.return_value = None  # Mock successful shutdown

        # Mock successful response from daemon
        response_json = json.dumps(
            {
                "success": True,
                "status": "success",
                "version": "abc123",
                "duration": 45.5,
                "error": None,
                "message": "Deployment completed",
            }
        ).encode("utf-8")

        # When wait=True, reads response: first call gets data, second empty
        mock_sock.recv.side_effect = [response_json, b""]

        runner.invoke(
            main,
            ["trigger-deploy", "api", "prod", "--wait"],
            obj={"skip_health": False},
        )

    @patch("os.execvp")
    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_trigger_deploy_follow_execs_journalctl(
        self, mock_get_config, mock_path_class, mock_socket_class, mock_execvp
    ):
        """Test trigger-deploy --follow execs into journalctl.

        The result is supplied because `--follow` implies `--wait`: since #356
        the logs are followed only once a real success has been reported. The
        empty-response case is covered by
        `TestFollowIsBoundByTheSameInvariant`, where it exits 1 instead.
        """
        runner = CliRunner()

        # Mock config
        config = MagicMock()
        config.project_name = "myproject"
        config.get_fraise_environment.return_value = {"type": "api"}
        mock_get_config.return_value = config

        # Mock Path
        mock_path = MagicMock()
        mock_path_class.return_value = mock_path
        mock_socket_path = MagicMock()
        mock_path.__truediv__.return_value = mock_socket_path
        mock_socket_path.__str__.return_value = (
            "/run/fraisier/myproject-prod/deploy.sock"
        )

        # Mock socket — a successful result, then empty bytes to break the loop
        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock
        mock_sock.connect.return_value = None
        mock_sock.sendall.return_value = None
        mock_sock.shutdown.return_value = None
        mock_sock.recv.side_effect = [
            json.dumps({"success": True, "message": "done"}).encode(),
            b"",
        ]

        runner.invoke(
            main,
            ["trigger-deploy", "api", "prod", "--follow"],
            obj={"skip_health": False},
        )

        # Should exec into journalctl
        mock_execvp.assert_called_once()
        args, _kwargs = mock_execvp.call_args
        assert args[0] == "journalctl"
        assert "-f" in args[1]  # follow flag
        assert "fraisier-api-prod@*.service" in args[1]


class TestTriggerDeployEstimate:
    """`fraisier trigger-deploy` prints an estimated completion line before
    dispatching, when a `database.strategy` is configured (#201 follow-up)."""

    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_prints_estimate_when_database_strategy_present(
        self, mock_get_config, mock_path_class, mock_socket_class
    ):
        runner = CliRunner()

        config = MagicMock()
        config.project_name = "myproject"
        config.get_fraise_environment.return_value = {
            "type": "api",
            "database": {"strategy": "rebuild"},
        }
        mock_get_config.return_value = config

        mock_path = MagicMock()
        mock_path_class.return_value = mock_path
        mock_socket_path = MagicMock()
        mock_path.__truediv__.return_value = mock_socket_path
        mock_socket_path.__str__.return_value = (
            "/run/fraisier/myproject-prod/deploy.sock"
        )

        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock
        mock_sock.connect.return_value = None
        mock_sock.sendall.return_value = None
        mock_sock.shutdown.return_value = None

        with patch(
            "fraisier.duration_estimate.build_estimate",
            return_value=EstimateResult(
                seconds=180, confidence="history", samples_used=5
            ),
        ):
            result = runner.invoke(
                main,
                ["trigger-deploy", "api", "prod"],
                obj={"skip_health": False},
            )

        assert result.exit_code == 0, result.output
        assert "Estimated completion: ~3m (history," in result.output
        assert "Deployment triggered successfully" in result.output

    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_prints_no_estimate_for_etl_fraise(
        self, mock_get_config, mock_path_class, mock_socket_class
    ):
        runner = CliRunner()

        config = MagicMock()
        config.project_name = "myproject"
        # No `database` section — ETL fraises return None from build_estimate.
        config.get_fraise_environment.return_value = {"type": "etl"}
        mock_get_config.return_value = config

        mock_path = MagicMock()
        mock_path_class.return_value = mock_path
        mock_socket_path = MagicMock()
        mock_path.__truediv__.return_value = mock_socket_path
        mock_socket_path.__str__.return_value = (
            "/run/fraisier/myproject-prod/deploy.sock"
        )

        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock
        mock_sock.connect.return_value = None
        mock_sock.sendall.return_value = None
        mock_sock.shutdown.return_value = None

        result = runner.invoke(
            main,
            ["trigger-deploy", "api", "prod"],
            obj={"skip_health": False},
        )

        assert result.exit_code == 0, result.output
        assert "Estimated completion:" not in result.output

    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_swallows_estimate_errors(
        self, mock_get_config, mock_path_class, mock_socket_class
    ):
        runner = CliRunner()

        config = MagicMock()
        config.project_name = "myproject"
        config.get_fraise_environment.return_value = {
            "type": "api",
            "database": {"strategy": "rebuild"},
        }
        mock_get_config.return_value = config

        mock_path = MagicMock()
        mock_path_class.return_value = mock_path
        mock_socket_path = MagicMock()
        mock_path.__truediv__.return_value = mock_socket_path
        mock_socket_path.__str__.return_value = (
            "/run/fraisier/myproject-prod/deploy.sock"
        )

        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock
        mock_sock.connect.return_value = None
        mock_sock.sendall.return_value = None
        mock_sock.shutdown.return_value = None

        with patch(
            "fraisier.duration_estimate.build_estimate",
            side_effect=RuntimeError("boom"),
        ):
            result = runner.invoke(
                main,
                ["trigger-deploy", "api", "prod"],
                obj={"skip_health": False},
            )

        assert result.exit_code == 0, result.output
        assert "Deployment triggered successfully" in result.output
        mock_sock.sendall.assert_called_once()


class TestTriggerDeployWaitOutcomeIsKnown:
    """--wait must not report success for an outcome it never received.

    A deploy unit whose socket is restarted under it dies without writing a
    result (#349). The connection then closes empty, which is indistinguishable
    from fire-and-forget unless --wait treats "no result" as a failure — the
    gap that let a nightly staging restore skip while systemd recorded
    ExecMainStatus=0.
    """

    @staticmethod
    def _wire(mock_get_config, mock_path_class, mock_socket_class):
        """Set up config, socket path and socket mocks; return the socket mock."""
        config = MagicMock()
        config.project_name = "myproject"
        config.get_fraise_environment.return_value = {"type": "api"}
        mock_get_config.return_value = config

        mock_path = MagicMock()
        mock_path_class.return_value = mock_path
        mock_socket_path = MagicMock()
        mock_path.__truediv__.return_value = mock_socket_path
        mock_socket_path.__str__.return_value = (
            "/run/fraisier/myproject-prod/deploy.sock"
        )

        mock_sock = MagicMock()
        mock_socket_class.return_value = mock_sock
        mock_sock.connect.return_value = None
        mock_sock.sendall.return_value = None
        mock_sock.shutdown.return_value = None
        return mock_sock

    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_wait_fails_when_no_result_arrives(
        self, mock_get_config, mock_path_class, mock_socket_class
    ):
        """An empty response under --wait exits non-zero, not 'triggered'."""
        runner = CliRunner()
        mock_sock = self._wire(mock_get_config, mock_path_class, mock_socket_class)
        mock_sock.recv.return_value = b""

        result = runner.invoke(
            main,
            ["trigger-deploy", "api", "prod", "--wait"],
            obj={"skip_health": False},
        )

        assert result.exit_code == 1, result.output
        assert "no result" in result.output
        assert "triggered successfully" not in result.output

    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_wait_fails_on_unparseable_result(
        self, mock_get_config, mock_path_class, mock_socket_class
    ):
        """Garbage on the wire is an unknown outcome, so it exits non-zero."""
        runner = CliRunner()
        mock_sock = self._wire(mock_get_config, mock_path_class, mock_socket_class)
        mock_sock.recv.side_effect = [b"not json at all", b""]

        result = runner.invoke(
            main,
            ["trigger-deploy", "api", "prod", "--wait"],
            obj={"skip_health": False},
        )

        assert result.exit_code == 1, result.output
        assert "triggered successfully" not in result.output

    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_wait_reports_a_failed_deployment(
        self, mock_get_config, mock_path_class, mock_socket_class
    ):
        """A result saying success=False still exits non-zero."""
        runner = CliRunner()
        mock_sock = self._wire(mock_get_config, mock_path_class, mock_socket_class)
        payload = json.dumps({"success": False, "error": "migrations failed"}).encode()
        mock_sock.recv.side_effect = [payload, b""]

        result = runner.invoke(
            main,
            ["trigger-deploy", "api", "prod", "--wait"],
            obj={"skip_health": False},
        )

        assert result.exit_code == 1, result.output
        assert "migrations failed" in result.output

    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_wait_reports_a_successful_deployment(
        self, mock_get_config, mock_path_class, mock_socket_class
    ):
        """The success path is untouched: a real result still exits 0."""
        runner = CliRunner()
        mock_sock = self._wire(mock_get_config, mock_path_class, mock_socket_class)
        payload = json.dumps(
            {"success": True, "message": "done", "version": "abc123", "duration": 4.2}
        ).encode()
        mock_sock.recv.side_effect = [payload, b""]

        result = runner.invoke(
            main,
            ["trigger-deploy", "api", "prod", "--wait"],
            obj={"skip_health": False},
        )

        assert result.exit_code == 0, result.output
        assert "Deployment successful" in result.output

    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_without_wait_an_empty_response_is_still_fire_and_forget(
        self, mock_get_config, mock_path_class, mock_socket_class
    ):
        """Without --wait the caller never asked for an outcome, so this is 0."""
        runner = CliRunner()
        mock_sock = self._wire(mock_get_config, mock_path_class, mock_socket_class)
        mock_sock.recv.return_value = b""

        result = runner.invoke(
            main,
            ["trigger-deploy", "api", "prod"],
            obj={"skip_health": False},
        )

        assert result.exit_code == 0, result.output
        assert "Deployment triggered successfully" in result.output

    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_empty_result_names_the_version_skew_remedy(
        self, mock_get_config, mock_path_class, mock_socket_class
    ):
        """The CLI upgrades itself long before the units are re-rendered.

        A deploy service older than v0.64.0 has no result channel at all, so
        every `--wait` on that host exits 1 until it is reinstalled. Telling
        the operator to retry would be telling them to wait forever; the
        message has to name the reinstall and the command that finds every
        affected unit.
        """
        runner = CliRunner()
        mock_sock = self._wire(mock_get_config, mock_path_class, mock_socket_class)
        mock_sock.recv.return_value = b""

        result = runner.invoke(
            main,
            ["trigger-deploy", "api", "prod", "--wait"],
            obj={"skip_health": False},
        )

        assert result.exit_code == 1, result.output
        assert "scaffold-install" in result.output
        assert "doctor" in result.output


class TestFollowIsBoundByTheSameInvariant:
    """`--follow` implies `--wait`, so it is a caller that asked for an outcome.

    It drained the result off the socket and then discarded it: the exec into
    journalctl happened before the result was looked at, so the process exited
    with journalctl's status, which says nothing about the deploy. A flag that
    implies `--wait` and then ignores the invariant is the next issue in this
    series.
    """

    @staticmethod
    def _wire(mock_get_config, mock_path_class, mock_socket_class):
        return TestTriggerDeployWaitOutcomeIsKnown._wire(
            mock_get_config, mock_path_class, mock_socket_class
        )

    @patch("fraisier.cli._deploy.os.execvp")
    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_follow_does_not_tail_logs_for_a_failed_deployment(
        self, mock_get_config, mock_path_class, mock_socket_class, mock_execvp
    ):
        """A failed deploy exits 1 rather than handing the shell to journalctl."""
        runner = CliRunner()
        mock_sock = self._wire(mock_get_config, mock_path_class, mock_socket_class)
        payload = json.dumps({"success": False, "error": "migrations failed"}).encode()
        mock_sock.recv.side_effect = [payload, b""]

        result = runner.invoke(
            main,
            ["trigger-deploy", "api", "prod", "--follow"],
            obj={"skip_health": False},
        )

        assert result.exit_code == 1, result.output
        assert "migrations failed" in result.output
        mock_execvp.assert_not_called()

    @patch("fraisier.cli._deploy.os.execvp")
    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_follow_does_not_tail_logs_when_no_result_arrives(
        self, mock_get_config, mock_path_class, mock_socket_class, mock_execvp
    ):
        """An unknown outcome under --follow is still an unknown outcome."""
        runner = CliRunner()
        mock_sock = self._wire(mock_get_config, mock_path_class, mock_socket_class)
        mock_sock.recv.return_value = b""

        result = runner.invoke(
            main,
            ["trigger-deploy", "api", "prod", "--follow"],
            obj={"skip_health": False},
        )

        assert result.exit_code == 1, result.output
        assert "triggered successfully" not in result.output
        mock_execvp.assert_not_called()

    @patch("fraisier.cli._deploy.os.execvp")
    @patch("socket.socket")
    @patch("fraisier.cli._deploy.Path")
    @patch("fraisier.cli.main.get_config")
    def test_follow_reports_the_outcome_then_tails_the_logs(
        self, mock_get_config, mock_path_class, mock_socket_class, mock_execvp
    ):
        """On success the summary is printed first — exec would erase it."""
        runner = CliRunner()
        mock_sock = self._wire(mock_get_config, mock_path_class, mock_socket_class)
        payload = json.dumps(
            {"success": True, "message": "done", "version": "abc123", "duration": 4.2}
        ).encode()
        mock_sock.recv.side_effect = [payload, b""]

        result = runner.invoke(
            main,
            ["trigger-deploy", "api", "prod", "--follow"],
            obj={"skip_health": False},
        )

        assert "Deployment successful" in result.output
        mock_execvp.assert_called_once()
        argv = mock_execvp.call_args[0][1]
        assert argv[:2] == ["journalctl", "-u"]
        assert argv[2] == "fraisier-api-prod@*.service"
        assert argv[3] == "-f"

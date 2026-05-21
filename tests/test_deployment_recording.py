"""Tests for deployment recording — DB + status file at each state transition."""

from unittest.mock import MagicMock, patch

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentResult, DeploymentStatus
from fraisier.deployers.etl import ETLDeployer
from fraisier.deployers.scheduled import ScheduledDeployer


class TestAPIDeployerRecording:
    """Test that APIDeployer records deployments in DB + status file."""

    def _make_deployer(self, **overrides):
        config = {
            "fraise_name": "my_api",
            "environment": "production",
            "app_path": "/var/www/api",
            "clone_url": "git@github.com:org/api.git",
            "repos_base": "/tmp/repos",
            "systemd_service": "api.service",
            **overrides,
        }
        return APIDeployer(config)

    def test_successful_deploy_records_in_database(self, test_db):
        """Successful deploy calls start_deployment + complete_deployment."""
        deployer = self._make_deployer()

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa111", "bbb222"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = deployer.execute()

        assert result.success is True

        # Verify DB recorded the deployment
        deployments = test_db.get_recent_deployments(fraise="my_api")
        assert len(deployments) == 1
        dep = deployments[0]
        assert dep["status"] == "success"
        assert dep["fraise_name"] == "my_api"
        assert dep["environment_name"] == "production"
        assert dep["new_version"] is not None
        assert dep["duration_seconds"] > 0

    def test_failed_deploy_records_in_database(self, test_db):
        """Failed deploy records failure with error message."""
        from subprocess import CalledProcessError

        deployer = self._make_deployer()

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                side_effect=CalledProcessError(1, "git fetch"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
        ):
            result = deployer.execute()

        assert result.success is False

        deployments = test_db.get_recent_deployments(fraise="my_api")
        assert len(deployments) == 1
        dep = deployments[0]
        assert dep["status"] == "failed"
        assert dep["error_message"] is not None

    def test_status_file_written_at_each_transition(self):
        """Status file is written at deploying, then success."""
        deployer = self._make_deployer()

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ),
            patch("fraisier.deployers.mixins.write_status") as mock_ws,
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            deployer.execute()

        states = [call.args[0].state for call in mock_ws.call_args_list]
        assert states[0] == "deploying"
        assert states[-1] == "success"

    def test_status_file_failed_on_error(self):
        """Status file transitions to 'failed' on error."""
        from subprocess import CalledProcessError

        deployer = self._make_deployer()

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                side_effect=CalledProcessError(1, "git"),
            ),
            patch("fraisier.deployers.mixins.write_status") as mock_ws,
        ):
            deployer.execute()

        states = [call.args[0].state for call in mock_ws.call_args_list]
        assert "deploying" in states
        assert "failed" in states


class TestETLDeployerRecording:
    """Test that ETLDeployer records deployments in DB + status file."""

    def _make_deployer(self, **overrides):
        config = {
            "fraise_name": "pipeline",
            "environment": "production",
            "app_path": "/var/etl",
            "repos_base": "/tmp/repos",
            **overrides,
        }
        return ETLDeployer(config)

    def test_successful_deploy_records_in_database(self, test_db):
        """Successful ETL deploy is recorded in DB."""
        deployer = self._make_deployer()

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
        ):
            result = deployer.execute()

        assert result.success is True

        deployments = test_db.get_recent_deployments(fraise="pipeline")
        assert len(deployments) == 1
        dep = deployments[0]
        assert dep["status"] == "success"
        assert dep["fraise_name"] == "pipeline"

    def test_failed_deploy_records_in_database(self, test_db):
        """Failed ETL deploy records failure."""
        from subprocess import CalledProcessError

        deployer = self._make_deployer(script_path="scripts/run.py")

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = CalledProcessError(1, "python")
            result = deployer.execute()

        assert result.success is False

        deployments = test_db.get_recent_deployments(fraise="pipeline")
        assert len(deployments) == 1
        assert deployments[0]["status"] == "failed"

    def test_status_file_transitions(self):
        """ETL deployer writes deploying → success status."""
        deployer = self._make_deployer()

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("aaa", "bbb"),
            ),
            patch("fraisier.deployers.mixins.write_status") as mock_ws,
        ):
            deployer.execute()

        states = [call.args[0].state for call in mock_ws.call_args_list]
        assert states == ["deploying", "success"]


class TestScheduledDeployerRecording:
    """Test that ScheduledDeployer records deployments in DB + status file."""

    def _make_deployer(self, **overrides):
        config = {
            "fraise_name": "backup",
            "environment": "production",
            "systemd_timer": "backup.timer",
            **overrides,
        }
        return ScheduledDeployer(config)

    def test_successful_deploy_records_in_database(self, test_db):
        """Successful scheduled deploy is recorded in DB."""
        deployer = self._make_deployer()

        with (
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="timer:active\n")
            result = deployer.execute()

        assert result.success is True

        deployments = test_db.get_recent_deployments(fraise="backup")
        assert len(deployments) == 1
        dep = deployments[0]
        assert dep["status"] == "success"
        assert dep["fraise_name"] == "backup"

    def test_failed_deploy_records_in_database(self, test_db):
        """Failed scheduled deploy records failure."""
        from subprocess import CalledProcessError

        deployer = self._make_deployer()

        with (
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = CalledProcessError(1, "systemctl")
            result = deployer.execute()

        assert result.success is False

        deployments = test_db.get_recent_deployments(fraise="backup")
        assert len(deployments) == 1
        assert deployments[0]["status"] == "failed"

    def test_status_file_transitions(self):
        """Scheduled deployer writes deploying → success status."""
        deployer = self._make_deployer()

        with (
            patch("fraisier.deployers.mixins.write_status") as mock_ws,
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="timer:active\n")
            deployer.execute()

        states = [call.args[0].state for call in mock_ws.call_args_list]
        assert states == ["deploying", "success"]


class TestRecordingConsistency:
    """Test that recording is consistent across deployer types."""

    def test_all_deployers_record_duration(self, test_db):
        """All deployer types record duration_seconds."""
        # API
        api = APIDeployer(
            {
                "fraise_name": "api",
                "environment": "prod",
                "app_path": "/app",
                "repos_base": "/tmp/repos",
            }
        )
        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("a", "b"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            api.execute()

        # ETL
        etl = ETLDeployer(
            {
                "fraise_name": "etl",
                "environment": "prod",
                "app_path": "/etl",
                "repos_base": "/tmp/repos",
            }
        )
        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("a", "b"),
            ),
            patch("fraisier.deployers.mixins.write_status"),
        ):
            etl.execute()

        # Scheduled
        sched = ScheduledDeployer(
            {
                "fraise_name": "sched",
                "environment": "prod",
                "systemd_timer": "sched.timer",
            }
        )
        with (
            patch("fraisier.deployers.mixins.write_status"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            sched.execute()

        for fraise in ["api", "etl", "sched"]:
            deps = test_db.get_recent_deployments(fraise=fraise)
            assert len(deps) == 1
            assert deps[0]["duration_seconds"] is not None
            assert deps[0]["duration_seconds"] >= 0


class TestCompleteDbRecordSizeSampling:
    """`_complete_db_record` samples pg_database_size and forwards
    db_size_mb to complete_deployment (#201 follow-up)."""

    def _api_deployer(self, **db_overrides):
        config = {
            "fraise_name": "my_api",
            "environment": "production",
            "app_path": "/var/www/api",
            "repos_base": "/tmp/repos",
        }
        if db_overrides is not None:
            config["database"] = db_overrides
        return APIDeployer(config)

    def _success_result(self) -> DeploymentResult:
        return DeploymentResult(
            success=True,
            status=DeploymentStatus.SUCCESS,
            new_version="v2",
        )

    def _failure_result(self) -> DeploymentResult:
        return DeploymentResult(
            success=False,
            status=DeploymentStatus.FAILED,
            error_message="boom",
        )

    def test_passes_db_size_mb_on_success(self):
        deployer = self._api_deployer(
            database_url="postgresql://u@h/db", strategy="rebuild"
        )
        with (
            patch("fraisier.database.get_db") as mock_get_db,
            patch(
                "fraisier.dbops.sizing.query_database_size_mb", return_value=42
            ) as mock_size,
        ):
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            deployer._complete_db_record(123, self._success_result())

        mock_size.assert_called_once()
        kwargs = mock_db.complete_deployment.call_args.kwargs
        assert kwargs["db_size_mb"] == 42
        assert kwargs["strategy"] == "rebuild"
        assert kwargs["success"] is True

    def test_passes_none_when_url_missing(self):
        # Fraise has database config (so strategy is recorded) but no
        # database_url — the size lookup must not fire.
        deployer = self._api_deployer(strategy="migrate")
        with (
            patch("fraisier.database.get_db") as mock_get_db,
            patch(
                "fraisier.dbops.sizing.query_database_size_mb"
            ) as mock_size,
        ):
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            deployer._complete_db_record(123, self._success_result())

        mock_size.assert_not_called()
        kwargs = mock_db.complete_deployment.call_args.kwargs
        assert kwargs["db_size_mb"] is None
        assert kwargs["strategy"] == "migrate"

    def test_passes_none_on_failure(self):
        deployer = self._api_deployer(
            database_url="postgresql://u@h/db", strategy="rebuild"
        )
        with (
            patch("fraisier.database.get_db") as mock_get_db,
            patch(
                "fraisier.dbops.sizing.query_database_size_mb"
            ) as mock_size,
        ):
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            deployer._complete_db_record(123, self._failure_result())

        # A failed deploy may have left the DB in an intermediate state;
        # do not sample its size.
        mock_size.assert_not_called()
        kwargs = mock_db.complete_deployment.call_args.kwargs
        assert kwargs["db_size_mb"] is None

    def test_swallows_sizing_exception(self):
        # query_database_size_mb is best-effort and should never raise,
        # but the recorder must remain robust if a future caller does.
        deployer = self._api_deployer(
            database_url="postgresql://u@h/db", strategy="rebuild"
        )
        with (
            patch("fraisier.database.get_db") as mock_get_db,
            patch(
                "fraisier.dbops.sizing.query_database_size_mb",
                side_effect=RuntimeError("unexpected"),
            ),
        ):
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            # Must not raise.
            deployer._complete_db_record(123, self._success_result())

        kwargs = mock_db.complete_deployment.call_args.kwargs
        assert kwargs["db_size_mb"] is None
        # The strategy column must still record even when the size lookup
        # blew up — inner try/except, not an outer wrap.
        assert kwargs["strategy"] == "rebuild"

    def test_passes_none_for_deployer_without_database_section(self):
        # An ETL fraise without a `database` config — database_config is an
        # empty dict, so neither strategy nor database_url is set, and the
        # sizing helper must not fire.
        deployer = ETLDeployer(
            {
                "fraise_name": "etl",
                "environment": "production",
                "app_path": "/var/etl",
                "repos_base": "/tmp/repos",
            }
        )
        with (
            patch("fraisier.database.get_db") as mock_get_db,
            patch(
                "fraisier.dbops.sizing.query_database_size_mb"
            ) as mock_size,
        ):
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            deployer._complete_db_record(123, self._success_result())

        mock_size.assert_not_called()
        kwargs = mock_db.complete_deployment.call_args.kwargs
        assert kwargs["db_size_mb"] is None

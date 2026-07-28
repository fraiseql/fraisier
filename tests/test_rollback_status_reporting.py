"""A deploy must report what its automatic rollback actually did (#293).

#272 made the DB rollback *run* when a migration batch partially applies. It
still reported ``FAILED``, because ``_restore_previous_state`` returned ``None``
and its only caller unconditionally built ``FAILED`` — so an operator could not
tell "schema rolled back cleanly" from "schema left dirty". The incident message
was lost the same way: ``_rollback_database`` wrote it to the status file and the
failure handler then overwrote it with the original migration error.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.base import DeploymentStatus
from fraisier.strategies import StrategyResult


def _deployer(tmp_path, **overrides):
    """An APIDeployer wired for a migrate-strategy deploy."""
    app_dir = tmp_path / "myapi"
    app_dir.mkdir(exist_ok=True)
    config = {
        "fraise_name": "myapi",
        "environment": "production",
        "app_path": str(app_dir),
        "clone_url": "git@github.com:org/myapi.git",
        "branch": "main",
        "systemd_service": "myapi.service",
        "database": {"strategy": "migrate", "confiture_config": "confiture.yaml"},
        "repos_base": str(tmp_path / "repos"),
        "status_dir": str(tmp_path / "status"),
        **overrides,
    }
    return APIDeployer(config)


def _restore(deployer, rollback_result: StrategyResult | None):
    """Run _restore_previous_state against a stubbed strategy, return its outcome."""
    strategy = MagicMock()
    if rollback_result is not None:
        strategy.rollback.return_value = rollback_result

    with (
        patch("fraisier.strategies.get_strategy", return_value=strategy),
        patch("subprocess.run"),
        patch.object(deployer, "_restart_service"),
        patch.object(deployer, "_write_incident"),
    ):
        return deployer._restore_previous_state()


class TestRestoreOutcomeIsReported:
    """_restore_previous_state must tell its caller what it did."""

    def test_no_previous_sha_reports_nothing_attempted(self, tmp_path):
        """Without a previous SHA nothing is restored, so nothing is claimed."""
        deployer = _deployer(tmp_path)
        deployer._previous_sha = None
        deployer._migrations_applied = 2

        outcome = _restore(deployer, None)

        assert outcome.db_rollback_attempted is False
        assert outcome.error_message is None

    def test_successful_db_rollback_is_recorded(self, tmp_path):
        """2 applied, 2 undone → attempted and succeeded."""
        deployer = _deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 2

        outcome = _restore(deployer, StrategyResult(success=True, migrations_applied=2))

        assert outcome.db_rollback_attempted is True
        assert outcome.db_rollback_succeeded is True
        assert outcome.error_message is None

    def test_failed_db_rollback_carries_the_incident_message(self, tmp_path):
        """The 'do not restart' text must reach the caller, not just the log."""
        deployer = _deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 3

        outcome = _restore(
            deployer,
            StrategyResult(
                success=False, migrations_applied=1, errors=["down 002 failed"]
            ),
        )

        assert outcome.db_rollback_attempted is True
        assert outcome.db_rollback_succeeded is False
        assert outcome.error_message is not None
        assert "1 of 3" in outcome.error_message
        assert "Do NOT restart" in outcome.error_message

    def test_git_only_restore_reports_no_db_attempt(self, tmp_path):
        """0 migrations applied → git revert only, no DB rollback claimed."""
        deployer = _deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 0

        outcome = _restore(deployer, None)

        assert outcome.db_rollback_attempted is False
        assert outcome.error_message is None

    def test_rollback_raising_is_reported_as_failure(self, tmp_path):
        """An exception mid-rollback must not read as a clean revert."""
        deployer = _deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 2

        strategy = MagicMock()
        strategy.rollback.side_effect = RuntimeError("connection lost")

        with (
            patch("fraisier.strategies.get_strategy", return_value=strategy),
            patch("subprocess.run"),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_write_incident"),
        ):
            outcome = deployer._restore_previous_state()

        assert outcome.db_rollback_attempted is True
        assert outcome.db_rollback_succeeded is False
        assert outcome.error_message is not None
        assert "connection lost" in outcome.error_message


class TestDeployReportsTheRollback:
    """The failure handler must map the outcome onto the deploy status."""

    def _failing_deploy(self, deployer, rollback_result: StrategyResult | None):
        """Drive execute() to failure at the git-pull step."""
        strategy = MagicMock()
        if rollback_result is not None:
            strategy.rollback.return_value = rollback_result

        with (
            patch.object(
                deployer, "_git_pull", side_effect=RuntimeError("pull exploded")
            ),
            patch.object(deployer, "_validate_wrapper_scripts"),
            patch.object(deployer, "_sync_config_if_needed"),
            patch.object(deployer, "_check_service_file_staleness"),
            patch.object(deployer, "_start_db_record", return_value=None),
            patch.object(deployer, "_complete_db_record"),
            patch.object(deployer, "_notify"),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_write_incident"),
            patch("fraisier.strategies.get_strategy", return_value=strategy),
            patch("subprocess.run"),
        ):
            return deployer.execute()

    def test_reports_rolled_back_when_db_rollback_succeeds(self, tmp_path):
        """A clean schema revert is not the same outcome as a bare failure."""
        deployer = _deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 2

        result = self._failing_deploy(
            deployer, StrategyResult(success=True, migrations_applied=2)
        )

        assert result.success is False
        assert result.status == DeploymentStatus.ROLLED_BACK

    def test_reports_rollback_failed_when_db_rollback_fails(self, tmp_path):
        """A dirty schema must be distinguishable from a clean revert."""
        deployer = _deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 3

        result = self._failing_deploy(
            deployer,
            StrategyResult(
                success=False, migrations_applied=1, errors=["down 002 failed"]
            ),
        )

        assert result.success is False
        assert result.status == DeploymentStatus.ROLLBACK_FAILED

    def test_git_only_failure_still_reports_failed(self, tmp_path):
        """Blast-radius pin: no DB rollback attempted → status is unchanged.

        _restore_previous_state runs on every deploy failure that has a previous
        SHA, git-reverting even when no migrations ran. Promoting those to
        ROLLED_BACK would change the reported status of nearly every failed
        deploy. Deliberately out of scope for #293 — see the phase README.
        """
        deployer = _deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 0

        result = self._failing_deploy(deployer, None)

        assert result.status == DeploymentStatus.FAILED

    def test_no_previous_sha_still_reports_failed(self, tmp_path):
        """Nothing was restored, so nothing is claimed."""
        deployer = _deployer(tmp_path)
        deployer._previous_sha = None
        deployer._migrations_applied = 2

        result = self._failing_deploy(deployer, None)

        assert result.status == DeploymentStatus.FAILED


class TestIncidentMessageSurvives:
    """The 'do not restart' text must reach the status file, not be overwritten."""

    def _status_after_failed_rollback(self, tmp_path):
        deployer = _deployer(tmp_path)
        deployer._previous_sha = "prev123"
        deployer._migrations_applied = 3

        strategy = MagicMock()
        strategy.rollback.return_value = StrategyResult(
            success=False, migrations_applied=1, errors=["down 002 failed"]
        )

        with (
            patch.object(
                deployer, "_git_pull", side_effect=RuntimeError("pull exploded")
            ),
            patch.object(deployer, "_validate_wrapper_scripts"),
            patch.object(deployer, "_sync_config_if_needed"),
            patch.object(deployer, "_check_service_file_staleness"),
            patch.object(deployer, "_start_db_record", return_value=None),
            patch.object(deployer, "_complete_db_record"),
            patch.object(deployer, "_notify"),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_write_incident"),
            patch("fraisier.strategies.get_strategy", return_value=strategy),
            patch("subprocess.run"),
            patch.object(deployer, "_write_status") as mock_status,
        ):
            result = deployer.execute()
        return result, mock_status

    def test_status_file_keeps_the_incident_message(self, tmp_path):
        """The last status write must carry the rollback text, not str(e)."""
        _, mock_status = self._status_after_failed_rollback(tmp_path)

        failure_writes = [
            call
            for call in mock_status.call_args_list
            if call.args and call.args[0] != "deploying"
        ]
        assert failure_writes, "expected a terminal status write"
        message = failure_writes[-1].kwargs.get("error_message", "")
        assert "Do NOT restart" in message
        assert "pull exploded" not in message

    def test_result_error_message_carries_the_incident_text(self, tmp_path):
        """The returned result must say the schema is dirty, too."""
        result, _ = self._status_after_failed_rollback(tmp_path)

        assert result.error_message is not None
        assert "Do NOT restart" in result.error_message


class TestRollbackStatesAreRenderable:
    """`fraisier status` must not swallow the states the deployer writes.

    ``rolled_back`` has been written since before #293 (api.py `_finalize_rollback`
    and the timeout path) and fell through to version comparison. A new
    ``rollback_failed`` would hit the same hole — the worst possible state to
    render silently, because the schema is dirty and the service must not restart.
    """

    def _state(self, state: str) -> str:
        from fraisier.cli._info import _compute_deployment_state
        from fraisier.status import DeploymentStatusFile

        status = DeploymentStatusFile(
            fraise_name="myfraise", environment="production", state=state
        )
        with patch("fraisier.cli._info.read_status", return_value=status):
            return _compute_deployment_state("myfraise", "abc1234", "abc1234")

    def test_rollback_failed_renders_loudly(self):
        """A dirty schema must be visible and must not read as merely failed."""
        rendered = self._state("rollback_failed")

        assert "rollback" in rendered.lower()
        assert "red" in rendered

    def test_rolled_back_renders_instead_of_falling_through(self):
        """Pre-existing gap: rolled_back reached the version-comparison fallback."""
        rendered = self._state("rolled_back")

        assert "rolled back" in rendered.lower().replace("_", " ")
        assert "idle" not in rendered

    def test_failed_still_renders_as_before(self):
        """Neither new branch may capture the ordinary failed state."""
        assert self._state("failed") == "[red]failed[/red]"

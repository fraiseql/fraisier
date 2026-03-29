"""Tests for the ship pipeline module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from rich.console import Console

from fraisier.config import ShipCheckConfig, ShipConfig
from fraisier.ship.checks import CheckResult, run_check
from fraisier.ship.pipeline import ShipPipeline


def _make_check(
    name: str = "test-check",
    command: list[str] | None = None,
    phase: str = "validate",
    triggers: list[str] | None = None,
    timeout: int = 60,
) -> ShipCheckConfig:
    return ShipCheckConfig(
        name=name,
        command=command or ["echo", "ok"],
        phase=phase,
        triggers=triggers,
        timeout=timeout,
    )


class TestRunCheck:
    """Test individual check execution."""

    @patch("fraisier.ship.checks.subprocess.run")
    def test_successful_check(self, mock_run):
        """Passing check returns success."""
        mock_run.return_value = MagicMock(returncode=0, stdout="all good", stderr="")
        check = _make_check()
        result = run_check(check, Path())
        assert result.success
        assert result.name == "test-check"
        assert result.duration_seconds >= 0

    @patch("fraisier.ship.checks.subprocess.run")
    def test_failing_check(self, mock_run):
        """Failing check returns failure with output."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error!")
        check = _make_check()
        result = run_check(check, Path())
        assert not result.success
        assert "error!" in result.output

    @patch("fraisier.ship.checks.subprocess.run")
    def test_timeout_check(self, mock_run):
        """Timed-out check returns failure."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["echo"], timeout=5)
        check = _make_check(timeout=5)
        result = run_check(check, Path())
        assert not result.success
        assert "Timed out" in result.output


class TestShipPipeline:
    """Test the phased pipeline orchestrator."""

    def _make_pipeline(
        self,
        checks: list[ShipCheckConfig] | None = None,
        parallel: bool = True,
    ) -> ShipPipeline:
        config = ShipConfig(
            checks=checks or [],
            parallel=parallel,
        )
        return ShipPipeline(
            config=config,
            cwd=Path(),
            console=Console(quiet=True),
        )

    @patch("fraisier.ship.pipeline.run_check")
    def test_fix_phase_runs_only_fix_checks(self, mock_check):
        """Fix phase only runs checks with phase='fix'."""
        mock_check.return_value = CheckResult(
            name="x", success=True, output="", duration_seconds=0.1
        )
        pipeline = self._make_pipeline(
            checks=[
                _make_check(name="fixer", phase="fix"),
                _make_check(name="validator", phase="validate"),
                _make_check(name="tester", phase="test"),
            ]
        )
        result = pipeline.run_fix_phase()
        assert result.success
        assert mock_check.call_count == 1
        called_check = mock_check.call_args[0][0]
        assert called_check.name == "fixer"

    @patch("fraisier.ship.pipeline.run_check")
    def test_verify_phase_runs_validate_and_test(self, mock_check):
        """Verify phase runs both validate and test checks."""
        mock_check.return_value = CheckResult(
            name="x", success=True, output="", duration_seconds=0.1
        )
        pipeline = self._make_pipeline(
            checks=[
                _make_check(name="fixer", phase="fix"),
                _make_check(name="validator", phase="validate"),
                _make_check(name="tester", phase="test"),
            ]
        )
        result = pipeline.run_verify_phase()
        assert result.success
        assert mock_check.call_count == 2
        called_names = {c[0][0].name for c in mock_check.call_args_list}
        assert called_names == {"validator", "tester"}

    @patch("fraisier.ship.pipeline.run_check")
    def test_fix_failure_returns_failed_result(self, mock_check):
        """Failed fix check returns failure with phase info."""
        mock_check.return_value = CheckResult(
            name="broken-fixer",
            success=False,
            output="error",
            duration_seconds=0.1,
        )
        pipeline = self._make_pipeline(
            checks=[_make_check(name="broken-fixer", phase="fix")]
        )
        result = pipeline.run_fix_phase()
        assert not result.success
        assert result.failed_phase == "fix"

    @patch("fraisier.ship.pipeline.run_check")
    def test_empty_phase_succeeds(self, mock_check):
        """Phase with no matching checks succeeds immediately."""
        pipeline = self._make_pipeline(checks=[_make_check(phase="test")])
        result = pipeline.run_fix_phase()
        assert result.success
        mock_check.assert_not_called()

    @patch("fraisier.ship.pipeline.run_check")
    def test_sequential_execution(self, mock_check):
        """Non-parallel pipeline runs checks sequentially."""
        mock_check.return_value = CheckResult(
            name="x", success=True, output="", duration_seconds=0.1
        )
        pipeline = self._make_pipeline(
            checks=[
                _make_check(name="a", phase="fix"),
                _make_check(name="b", phase="fix"),
            ],
            parallel=False,
        )
        result = pipeline.run_fix_phase()
        assert result.success
        assert mock_check.call_count == 2

    @patch("fraisier.ship.pipeline.ShipPipeline._get_changed_files")
    @patch("fraisier.ship.pipeline.run_check")
    def test_trigger_filtering_skips_unmatched(self, mock_check, mock_changed):
        """Checks with triggers are skipped when no files match."""
        mock_changed.return_value = ["src/main.py"]
        mock_check.return_value = CheckResult(
            name="x", success=True, output="", duration_seconds=0.1
        )
        pipeline = self._make_pipeline(
            checks=[
                _make_check(
                    name="db-lint",
                    phase="validate",
                    triggers=["db/**"],
                ),
            ]
        )
        result = pipeline.run_verify_phase()
        assert result.success
        mock_check.assert_not_called()

    @patch("fraisier.ship.pipeline.ShipPipeline._get_changed_files")
    @patch("fraisier.ship.pipeline.run_check")
    def test_trigger_filtering_runs_matched(self, mock_check, mock_changed):
        """Checks with triggers run when files match."""
        mock_changed.return_value = ["db/migrations/001.sql"]
        mock_check.return_value = CheckResult(
            name="x", success=True, output="", duration_seconds=0.1
        )
        pipeline = self._make_pipeline(
            checks=[
                _make_check(
                    name="db-lint",
                    phase="validate",
                    triggers=["db/**"],
                ),
            ]
        )
        result = pipeline.run_verify_phase()
        assert result.success
        mock_check.assert_called_once()

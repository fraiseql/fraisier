"""Tests for the ship pipeline module."""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from rich.console import Console

from fraisier.config import ShipCheckConfig, ShipConfig
from fraisier.ship.checks import CheckResult, run_check
from fraisier.ship.pipeline import ShipPipeline

if TYPE_CHECKING:
    import pytest


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


class TestCheckUntrackedMigrations:
    """Test the built-in untracked migration file check."""

    def _make_pipeline(self) -> ShipPipeline:
        config = ShipConfig(checks=[])
        return ShipPipeline(
            config=config,
            cwd=Path(),
            console=Console(quiet=True),
        )

    def test_missing_migrations_dir_passes(self, tmp_path: Path) -> None:
        """No error when migration directory does not exist."""
        pipeline = self._make_pipeline()
        result = pipeline.check_untracked_migrations(tmp_path / "db" / "migrations")
        assert result.success

    def test_empty_migrations_dir_passes(self, tmp_path: Path) -> None:
        """No error when migration directory is empty."""
        migrations = tmp_path / "db" / "migrations"
        migrations.mkdir(parents=True)
        pipeline = self._make_pipeline()
        result = pipeline.check_untracked_migrations(migrations)
        assert result.success

    @patch("fraisier.ship.pipeline.subprocess.run")
    def test_no_untracked_files_passes(self, mock_run: MagicMock) -> None:
        """Passes when git reports no untracked files."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        pipeline = self._make_pipeline()
        migrations = Path("db/migrations")
        with patch.object(Path, "is_dir", return_value=True):
            result = pipeline.check_untracked_migrations(migrations)
        assert result.success

    @patch("fraisier.ship.pipeline.subprocess.run")
    def test_untracked_files_detected(self, mock_run: MagicMock) -> None:
        """Fails when untracked migration files exist."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="db/migrations/003_add_users.sql\ndb/migrations/004_add_roles.sql\n",
        )
        pipeline = self._make_pipeline()
        migrations = Path("db/migrations")
        with patch.object(Path, "is_dir", return_value=True):
            result = pipeline.check_untracked_migrations(migrations)
        assert not result.success
        assert result.failed_phase == "untracked-migrations"
        assert len(result.results) == 1
        assert "003_add_users.sql" in result.results[0].output
        assert "004_add_roles.sql" in result.results[0].output

    @patch("fraisier.ship.pipeline.subprocess.run")
    def test_git_ls_files_called_with_correct_args(self, mock_run: MagicMock) -> None:
        """Verifies the correct git command is invoked."""
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        pipeline = self._make_pipeline()
        migrations = Path("db/migrations")
        with patch.object(Path, "is_dir", return_value=True):
            pipeline.check_untracked_migrations(migrations)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "db/migrations",
        ]


class TestPrintFailureOutput:
    """Failing-check output shows the tail (the verdict) and keeps a full log (#255)."""

    def _make_pipeline(self) -> tuple[ShipPipeline, Console]:
        console = Console(file=io.StringIO(), width=500)
        pipeline = ShipPipeline(
            config=ShipConfig(checks=[]),
            cwd=Path(),
            console=console,
        )
        return pipeline, console

    @staticmethod
    def _text(console: Console) -> str:
        assert isinstance(console.file, io.StringIO)
        return console.file.getvalue()

    def test_passing_check_prints_no_output(self) -> None:
        """A passing check prints only its status line, never its output."""
        pipeline, console = self._make_pipeline()
        pipeline._print_result(
            CheckResult(
                name="pytest", success=True, output="noise", duration_seconds=0.1
            )
        )
        text = self._text(console)
        assert "pass pytest" in text
        assert "noise" not in text

    def test_failure_shows_tail_not_head(self) -> None:
        """The verdict at the *end* of the output is shown; the head is dropped."""
        pipeline, console = self._make_pipeline()
        body = "\n".join(["STARTUP-BANNER", *[f"noise-{i}" for i in range(45)]])
        output = body + "\nFAILED-VERDICT: assert 1 == 2"
        pipeline._print_result(
            CheckResult(
                name="pytest", success=False, output=output, duration_seconds=2.0
            )
        )
        text = self._text(console)
        # The failure summary (tail) is what the operator needs — show it.
        assert "FAILED-VERDICT: assert 1 == 2" in text
        # The startup banner (head) is collection noise — drop it.
        assert "STARTUP-BANNER" not in text

    def test_truncation_note_and_full_log_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Long output is truncated in the console but preserved in full on disk."""
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        pipeline, console = self._make_pipeline()
        body = "\n".join(["STARTUP-BANNER", *[f"noise-{i}" for i in range(45)]])
        output = body + "\nFAILED-VERDICT: assert 1 == 2"
        pipeline._print_result(
            CheckResult(
                name="pytest", success=False, output=output, duration_seconds=2.0
            )
        )
        text = self._text(console)

        # A note tells the operator some lines were hidden and where to find them.
        assert "hidden" in text
        logs = list((tmp_path / "fraisier" / "logs").glob("*.log"))
        assert len(logs) == 1
        log_path = logs[0]
        # The console points at the full log...
        assert str(log_path) in text
        # ...and the log holds the *complete* output, head included.
        contents = log_path.read_text()
        assert "STARTUP-BANNER" in contents
        assert "FAILED-VERDICT: assert 1 == 2" in contents
        # 0o600 so a leaked token in the output isn't world-readable.
        assert (log_path.stat().st_mode & 0o777) == 0o600

    def test_short_output_shown_in_full_without_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Output that fits the window is shown whole — no note, no log file."""
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        pipeline, console = self._make_pipeline()
        output = "first line\nsecond line\nthird line"
        pipeline._print_result(
            CheckResult(name="ruff", success=False, output=output, duration_seconds=0.3)
        )
        text = self._text(console)
        assert "first line" in text
        assert "third line" in text
        assert "hidden" not in text
        assert not (tmp_path / "fraisier" / "logs").exists() or not list(
            (tmp_path / "fraisier" / "logs").glob("*.log")
        )

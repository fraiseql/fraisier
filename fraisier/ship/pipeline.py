"""Phased pipeline orchestrator for fraisier ship."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fraisier.ship.checks import CheckResult, run_check

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import Console

    from fraisier.config import ShipCheckConfig, ShipConfig

# Failed checks print their last N output lines — tools (pytest, ruff, mypy)
# put the verdict at the end, so the tail is what diagnoses the failure (#255).
_FAILURE_TAIL_LINES = 30


@dataclass
class PipelineResult:
    """Aggregate result of the full ship pipeline."""

    success: bool
    failed_phase: str | None = None
    results: list[CheckResult] = field(default_factory=list)
    duration_seconds: float = 0.0


class ShipPipeline:
    """Run ship checks in phases: fix → validate+test."""

    def __init__(
        self,
        config: ShipConfig,
        cwd: Path,
        console: Console,
    ) -> None:
        self._config = config
        self._cwd = cwd
        self._console = console

    def check_untracked_migrations(self, migrations_dir: Path) -> PipelineResult:
        """Fail if untracked files exist in the migrations directory.

        Prevents shipping a commit that references a migration file which
        was never ``git add``-ed.  ``git add --update`` (used by the ship
        pipeline) only stages *tracked* files, so a brand-new migration
        would be silently left behind.
        """
        if not migrations_dir.is_dir():
            return PipelineResult(success=True)

        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", str(migrations_dir)],
            cwd=self._cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        untracked = [line for line in result.stdout.strip().split("\n") if line]
        if not untracked:
            return PipelineResult(success=True)

        self._console.print("[red]Untracked migration files detected:[/red]")
        for path in untracked:
            self._console.print(f"  {path}")
        self._console.print(
            "\n[yellow]Why:[/yellow] git add --update only stages tracked files."
            " These new migrations would be silently left out of the commit,"
            " causing deployment failures when confiture migrate runs."
        )
        self._console.print(
            f"\nTo include them:  [bold]git add {' '.join(untracked)}[/bold]"
        )
        self._console.print("To ignore:        delete or .gitignore the files above.")

        return PipelineResult(
            success=False,
            failed_phase="untracked-migrations",
            results=[
                CheckResult(
                    name="untracked-migrations",
                    success=False,
                    output="\n".join(untracked),
                    duration_seconds=0.0,
                ),
            ],
        )

    def run_fix_phase(self) -> PipelineResult:
        """Run auto-fixer checks (before staging)."""
        return self._run_phase("fix")

    def run_verify_phase(self) -> PipelineResult:
        """Run validate + test checks concurrently (after staging)."""
        checks = [
            c
            for c in self._config.checks
            if c.phase in ("validate", "test") and self._should_run(c)
        ]
        if not checks:
            return PipelineResult(success=True)
        return self._execute_checks(checks, phase_label="validate+test")

    def _run_phase(self, phase: str) -> PipelineResult:
        checks = [
            c for c in self._config.checks if c.phase == phase and self._should_run(c)
        ]
        if not checks:
            return PipelineResult(success=True)
        return self._execute_checks(checks, phase_label=phase)

    def _execute_checks(
        self,
        checks: list[ShipCheckConfig],
        phase_label: str,
    ) -> PipelineResult:
        start = time.monotonic()
        results: list[CheckResult] = []

        if self._config.parallel and len(checks) > 1:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(run_check, c, self._cwd): c for c in checks}
                for future in futures:
                    result = future.result()
                    results.append(result)
                    self._print_result(result)
        else:
            for check in checks:
                result = run_check(check, self._cwd)
                results.append(result)
                self._print_result(result)

        duration = time.monotonic() - start
        failed = [r for r in results if not r.success]
        if failed:
            return PipelineResult(
                success=False,
                failed_phase=phase_label,
                results=results,
                duration_seconds=duration,
            )
        return PipelineResult(
            success=True,
            results=results,
            duration_seconds=duration,
        )

    def _should_run(self, check: ShipCheckConfig) -> bool:
        """Check if a triggered check should run based on changed files."""
        if check.triggers is None:
            return True
        changed = self._get_changed_files()
        if not changed:
            return False
        return any(
            fnmatch.fnmatch(f, pattern) for f in changed for pattern in check.triggers
        )

    def _get_changed_files(self) -> list[str]:
        """Get list of changed files (staged + unstaged)."""
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=self._cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.strip().split("\n") if line]

    def _print_result(self, result: CheckResult) -> None:
        status = "[green]pass[/green]" if result.success else "[red]FAIL[/red]"
        self._console.print(
            f"  {status} {result.name} ({result.duration_seconds:.1f}s)"
        )
        if not result.success and result.output:
            self._print_failure_output(result)

    def _print_failure_output(self, result: CheckResult) -> None:
        """Surface *why* a check failed (issue #255).

        Tools like pytest and ruff print their verdict at the **end** of
        their output, so the console shows the **tail** (not the head, which
        is startup/collection noise). When the tail hides earlier lines, the
        *full* output is written to a log file and its path is printed so
        nothing is lost.
        """
        lines = result.output.strip().split("\n")
        tail = lines[-_FAILURE_TAIL_LINES:]
        hidden = len(lines) - len(tail)
        if hidden > 0:
            note = f"... {hidden} earlier line(s) hidden"
            log_path = self._write_failure_log(result)
            if log_path is not None:
                note += f"; full output: {log_path}"
            self._console.print(f"    [dim]{note}[/dim]")
        for line in tail:
            self._console.print(f"    {line}")

    def _write_failure_log(self, result: CheckResult) -> Path | None:
        """Write a failed check's full output to a 0o600 log file; return its path.

        Best-effort: reuses the XDG-compliant log directory shared with the
        ``fraisier._output`` tee layer. Returns ``None`` if the directory or
        file cannot be created so a logging hiccup never masks the failure.
        """
        from fraisier._output import _log_dir, _now_stamp

        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", result.name).strip("-") or "check"
        try:
            log_dir = _log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"ship-check-{safe_name}-{_now_stamp()}.log"
            fd = os.open(
                str(log_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(fd, "w") as log_file:
                log_file.write(result.output)
        except OSError:
            return None
        return log_path

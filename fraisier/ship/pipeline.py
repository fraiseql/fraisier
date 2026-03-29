"""Phased pipeline orchestrator for fraisier ship."""

from __future__ import annotations

import fnmatch
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
            for line in result.output.strip().split("\n")[:10]:
                self._console.print(f"    {line}")

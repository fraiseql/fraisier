"""Individual check execution for the ship pipeline."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from fraisier.config import ShipCheckConfig


@dataclass
class CheckResult:
    """Result of a single ship check."""

    name: str
    success: bool
    output: str
    duration_seconds: float


def run_check(check: ShipCheckConfig, cwd: Path) -> CheckResult:
    """Run a single ship check as a subprocess.

    Args:
        check: Check configuration with command and timeout.
        cwd: Working directory for the check.

    Returns:
        CheckResult with success status, output, and timing.
    """
    start = time.monotonic()
    try:
        result = subprocess.run(
            check.command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=check.timeout,
            check=False,
        )
        duration = time.monotonic() - start
        output = result.stdout
        if result.stderr:
            output = f"{output}\n{result.stderr}".strip()
        return CheckResult(
            name=check.name,
            success=result.returncode == 0,
            output=output,
            duration_seconds=duration,
        )
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return CheckResult(
            name=check.name,
            success=False,
            output=f"Timed out after {check.timeout}s",
            duration_seconds=duration,
        )

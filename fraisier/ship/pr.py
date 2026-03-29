"""PR creation for fraisier ship."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console


def create_pr(
    version: str,
    base: str,
    console: Console,
) -> str | None:
    """Create a GitHub PR via gh CLI.

    Args:
        version: The version being shipped (e.g. "1.2.3").
        base: Base branch for the PR.
        console: Rich console for output.

    Returns:
        PR URL on success, None on failure.
    """
    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base,
            "--title",
            f"release: v{version}",
            "--body",
            f"Automated release of v{version} via `fraisier ship`.",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        url = result.stdout.strip()
        console.print(f"[green]PR created:[/green] {url}")
        return url
    console.print(f"[red]PR creation failed:[/red] {result.stderr.strip()}")
    return None

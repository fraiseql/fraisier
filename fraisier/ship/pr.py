"""PR operations for fraisier ship.

Provider-agnostic public API — delegates to a ``PRBackend`` resolved from the
configured git provider.  Adding support for a new provider means subclassing
``PRBackend`` and registering it in ``_BACKENDS``.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console


# ---------------------------------------------------------------------------
# Backend abstraction
# ---------------------------------------------------------------------------


class PRBackend(ABC):
    """Abstract PR operations for a specific git hosting provider."""

    @abstractmethod
    def create_pr(self, version: str, base: str, console: Console) -> str | None:
        """Create a PR and return its URL, or None on failure."""

    @abstractmethod
    def find_current_pr_url(self, console: Console) -> str | None:
        """Return the open PR URL for the current branch, or None."""

    @abstractmethod
    def enable_auto_merge(
        self, pr_url: str, merge_method: str, console: Console
    ) -> None:
        """Enable auto-merge on the given PR URL."""


class GitHubPRBackend(PRBackend):
    """PR operations via the ``gh`` CLI (github.com and GitHub Enterprise)."""

    def create_pr(self, version: str, base: str, console: Console) -> str | None:
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

    def find_current_pr_url(self, console: Console) -> str | None:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "url", "-q", ".url"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            if url:
                return url
        console.print(
            "[yellow]Auto-merge skipped:[/yellow] no open PR found for this branch. "
            "Create one first with [bold]--pr --pr-base <base>[/bold], "
            "or combine [bold]--auto-merge[/bold] with [bold]--pr[/bold]."
        )
        return None

    def enable_auto_merge(
        self, pr_url: str, merge_method: str, console: Console
    ) -> None:
        result = subprocess.run(
            ["gh", "pr", "merge", "--auto", f"--{merge_method}", pr_url],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            console.print(
                f"[green]Auto-merge enabled ({merge_method}):[/green] {pr_url}"
            )
        else:
            console.print(
                f"[yellow]Auto-merge not enabled:[/yellow] {result.stderr.strip()}"
            )


class UnimplementedPRBackend(PRBackend):
    """Fallback for providers that do not yet have CLI-based PR support."""

    def __init__(self, provider_name: str) -> None:
        self._provider = provider_name

    def _warn(self, console: Console, operation: str) -> None:
        console.print(
            f"[yellow]{operation} is not yet supported for provider "
            f"'{self._provider}'.[/yellow] "
            "Open a GitHub issue if you need this."
        )

    def create_pr(self, version: str, base: str, console: Console) -> str | None:
        self._warn(console, "PR creation")
        return None

    def find_current_pr_url(self, console: Console) -> str | None:
        return None

    def enable_auto_merge(
        self, pr_url: str, merge_method: str, console: Console
    ) -> None:
        self._warn(console, "Auto-merge")


# Map provider name → backend class.  Extend here to add new providers.
_BACKENDS: dict[str, type[PRBackend]] = {
    "github": GitHubPRBackend,
}


def get_pr_backend(provider_name: str) -> PRBackend:
    """Return the PR backend for *provider_name*, falling back to unsupported."""
    cls = _BACKENDS.get(provider_name)
    if cls is not None:
        return cls()
    return UnimplementedPRBackend(provider_name)


def _resolve_backend() -> PRBackend:
    """Resolve PR backend from the fraises.yaml git provider config."""
    try:
        from fraisier.config import get_config

        git_config = get_config().get_git_provider_config()
        provider_name = git_config.get("provider", "github")
    except FileNotFoundError:
        # No fraises.yaml found — default to GitHub.
        provider_name = "github"
    return get_pr_backend(provider_name)


# ---------------------------------------------------------------------------
# Public API — thin wrappers; version.py call sites are unchanged
# ---------------------------------------------------------------------------


def create_pr(
    version: str,
    base: str,
    console: Console,
    *,
    backend: PRBackend | None = None,
) -> str | None:
    """Create a PR for the shipped version.

    Args:
        version: Version being shipped (e.g. "1.2.3").
        base: Base branch for the PR.
        console: Rich console for output.
        backend: Override the provider backend (used in tests).

    Returns:
        PR URL on success, None on failure or unsupported provider.
    """
    return (backend or _resolve_backend()).create_pr(version, base, console)


def enable_auto_merge(
    merge_method: str,
    console: Console,
    *,
    pr_url: str | None = None,
    backend: PRBackend | None = None,
) -> None:
    """Enable auto-merge on a PR.

    When ``pr_url`` is provided (e.g. just created via ``--pr``), it is used
    directly.  When omitted, the backend asks the host whether an open PR
    already exists for the current branch, and emits an actionable hint if not.

    Args:
        merge_method: One of "squash", "merge", or "rebase".
        console: Rich console for output.
        pr_url: Known PR URL, or None to detect from the current branch.
        backend: Override the provider backend (used in tests).
    """
    resolved = backend or _resolve_backend()
    target = pr_url or resolved.find_current_pr_url(console)
    if target is None:
        return
    resolved.enable_auto_merge(target, merge_method, console)

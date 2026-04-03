"""Health polling for deployment verification."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from fraisier.cli._helpers import console


@dataclass
class PollResult:
    """Result of health polling operation."""

    success: bool
    final_version: str | None
    elapsed_seconds: float
    attempts: int


def poll_health_for_version(
    health_url: str,
    expected_version: str,
    timeout: int = 300,
    interval: int = 10,
    console_output: bool = True,
) -> PollResult:
    """Poll health endpoint until expected version is detected or timeout.

    Args:
        health_url: URL to poll for health/version info
        expected_version: Version string to wait for
        timeout: Maximum seconds to poll
        interval: Seconds between polls
        console_output: Whether to print progress to console

    Returns:
        PollResult with success status and final state
    """
    start_time = time.monotonic()
    attempts = 0
    last_version = None

    deadline = start_time + timeout

    while time.monotonic() < deadline:
        attempts += 1
        elapsed = time.monotonic() - start_time

        try:
            resp = httpx.get(health_url, timeout=5)
            resp.raise_for_status()

            data = resp.json()
            current_version = _extract_version_from_health_response(data)
            last_version = current_version

            if console_output:
                _print_poll_status(elapsed, current_version, expected_version)

            if current_version == expected_version:
                return PollResult(
                    success=True,
                    final_version=current_version,
                    elapsed_seconds=elapsed,
                    attempts=attempts,
                )

        except Exception as e:
            if console_output:
                console.print(f"[dim]Health check failed: {e}[/dim]")

        # Wait before next attempt
        time.sleep(interval)

    # Timeout reached
    return PollResult(
        success=False,
        final_version=last_version,
        elapsed_seconds=time.monotonic() - start_time,
        attempts=attempts,
    )


def _extract_version_from_health_response(data: dict) -> str | None:
    """Extract version from health check response.

    Tries common version field names used by applications.
    """
    for field in ["version", "app_version", "build", "build_version"]:
        if field in data:
            return str(data[field])
    return None


def _print_poll_status(
    elapsed: float, current_version: str | None, expected_version: str
) -> None:
    """Print polling status to console."""
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    time_str = f"{minutes:02d}:{seconds:02d}"

    version_str = current_version or "unknown"
    console.print(
        f"[{time_str}] current version: {version_str} — waiting for {expected_version}"
    )

"""Deployment status file — state machine readable by monitoring and CI.

Provides atomic write/read of deployment state as JSON files.
Uses temp file + rename for POSIX atomicity (no partial reads).
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from fraisier.errors import ValidationError

logger = logging.getLogger("fraisier")

DEFAULT_STATUS_DIR = Path("/var/lib/fraisier/status")

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _validate_fraise_name(name: str) -> None:
    """Reject fraise names that could cause path traversal."""
    if not _SAFE_NAME_RE.match(name):
        msg = f"Invalid fraise name: {name!r} — must match [a-zA-Z0-9_-]+"
        raise ValidationError(msg)


@dataclass
class DeploymentStatusFile:
    """Deployment status as a state machine.

    States: idle -> pending -> deploying -> success | failed
    """

    fraise_name: str
    environment: str
    state: str = "idle"
    version: str | None = None
    commit_sha: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None
    migration_report: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None


def write_status(
    status: DeploymentStatusFile,
    status_dir: Path = DEFAULT_STATUS_DIR,
) -> Path:
    """Write deployment status atomically (temp file + rename).

    Args:
        status: The deployment status to write.
        status_dir: Directory for status files.

    Returns:
        Path to the written status file.
    """
    _validate_fraise_name(status.fraise_name)
    status_dir.mkdir(parents=True, exist_ok=True)
    path = status_dir / f"{status.fraise_name}.status.json"

    # Write to temp file in same directory, then atomic rename
    _fd, tmp_path_str = tempfile.mkstemp(
        dir=status_dir, suffix=".tmp", prefix=f"{status.fraise_name}."
    )
    tmp_path = Path(tmp_path_str)
    try:
        tmp_path.write_text(json.dumps(asdict(status), indent=2))
        tmp_path.rename(path)  # Atomic on POSIX
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return path


def read_status(
    fraise_name: str,
    status_dir: Path = DEFAULT_STATUS_DIR,
) -> DeploymentStatusFile | None:
    """Read deployment status from file.

    Args:
        fraise_name: Name of the fraise.
        status_dir: Directory for status files.

    Returns:
        DeploymentStatusFile or None if no status file exists.
    """
    _validate_fraise_name(fraise_name)
    path = status_dir / f"{fraise_name}.status.json"
    if not path.exists():
        return None

    data = json.loads(path.read_text())
    return DeploymentStatusFile(**data)


def elapsed_seconds(status: DeploymentStatusFile) -> float | None:
    """Compute elapsed seconds since deployment started.

    Args:
        status: The deployment status file.

    Returns:
        Seconds elapsed since started_at, or None if not started or invalid.
    """
    if not status.started_at:
        return None

    try:
        # Parse ISO 8601 timestamp (assume UTC)
        import datetime

        if status.started_at.endswith("Z"):
            started_dt = datetime.datetime.fromisoformat(
                status.started_at[:-1]
            ).replace(tzinfo=datetime.UTC)
        else:
            started_dt = datetime.datetime.fromisoformat(status.started_at)
        started_ts = started_dt.timestamp()
        return time.time() - started_ts
    except (ValueError, TypeError, AttributeError):
        return None

    try:
        # Parse ISO 8601 timestamp (assume UTC)
        # Format: 2026-04-03T10:00:00+00:00 or 2026-04-03T10:00:00Z
        if status.started_at.endswith("Z"):
            started_ts = time.mktime(
                time.strptime(status.started_at[:-1], "%Y-%m-%dT%H:%M:%S")
            )
        else:
            # Remove timezone offset for simplicity (assume UTC)
            started_str = (
                status.started_at.split("+")[0].split("-")[-1]
                if "+" in status.started_at
                else status.started_at
            )
            started_ts = time.mktime(time.strptime(started_str, "%Y-%m-%dT%H:%M:%S"))

        return time.time() - started_ts
    except (ValueError, TypeError):
        return None

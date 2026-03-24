"""Deployment status file — state machine readable by monitoring and CI.

Provides atomic write/read of deployment state as JSON files.
Uses temp file + rename for POSIX atomicity (no partial reads).
"""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("fraisier")

DEFAULT_STATUS_DIR = Path("/var/lib/fraisier/status")


@dataclass
class DeploymentStatusFile:
    """Deployment status as a state machine.

    States: idle -> deploying -> success | failed
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
    path = status_dir / f"{fraise_name}.status.json"
    if not path.exists():
        return None

    data = json.loads(path.read_text())
    return DeploymentStatusFile(**data)

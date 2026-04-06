"""Drift detection for scaffolded files.

Detects when generated files have been modified after scaffolding,
helping identify untracked changes that may cause deployment issues.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DriftResult:
    """Result of checking a single file for drift."""

    name: str
    drifted: bool
    message: str = ""


def _hash_file(path: Path) -> str:
    """Compute sha256 hash of a file."""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def detect_drift(
    output_dir: Path,
    template_hashes: dict[str, str],
    ignore: set[str] | None = None,
) -> list[DriftResult]:
    """Detect files that have drifted from their scaffolded templates.

    Args:
        output_dir: Directory containing generated files.
        template_hashes: Mapping of filename -> expected "sha256:..." hash.
        ignore: Set of filenames to skip (opt-out per file).

    Returns:
        List of DriftResult for files that have drifted.
    """
    ignore = ignore or set()
    drifted: list[DriftResult] = []

    for filename, expected_hash in template_hashes.items():
        if filename in ignore:
            continue

        file_path = output_dir / filename
        if not file_path.exists():
            drifted.append(
                DriftResult(
                    name=filename,
                    drifted=True,
                    message=f"Missing: {filename} not found in {output_dir}",
                )
            )
            continue

        actual_hash = _hash_file(file_path)
        if actual_hash != expected_hash:
            drifted.append(
                DriftResult(
                    name=filename,
                    drifted=True,
                    message=f"Modified: {filename} differs from template",
                )
            )

    return drifted

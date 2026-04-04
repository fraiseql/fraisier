"""Scaffold drift detection and diffing."""

from __future__ import annotations

import difflib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fraisier.config import FraisierConfig


@dataclass
class FileDiff:
    """Represents the difference between generated scaffold and installed files."""

    generated_path: str  # Relative scaffold path (e.g., "systemd/myapp.service")
    installed_path: (
        Path  # Absolute system path (e.g., /etc/systemd/system/myapp.service)
    )
    status: Literal["match", "differs", "missing_installed", "missing_generated"]
    diff_lines: list[str] | None = None  # Unified diff lines, None if match


def compute_scaffold_diff(
    config: FraisierConfig,
    server: str | None = None,
    fraise_filter: str | None = None,
    env_filter: str | None = None,
) -> list[FileDiff]:
    """Compare generated scaffold against installed files.

    Args:
        config: Fraisier configuration
        server: Optional server filter
        fraise_filter: Optional fraise name filter
        env_filter: Optional environment filter

    Returns:
        List of FileDiff objects representing differences
    """
    from fraisier.naming import deploy_socket_name
    from fraisier.scaffold.renderer import ScaffoldRenderer

    # Pre-compute the set of deploy unit paths that match the filters.
    # This replaces filename-parsing since new names don't encode fraise/env.
    matching_deploy_paths: set[str] = set()
    if fraise_filter or env_filter:
        for fraise_name, fraise_cfg in config.fraises.items():
            if fraise_filter and fraise_name != fraise_filter:
                continue
            for env_key, env_cfg in fraise_cfg.get("environments", {}).items():
                if env_filter and env_key != env_filter:
                    continue
                socket_unit = deploy_socket_name(env_cfg, env_key)
                socket_stem = socket_unit.removesuffix(".socket")
                matching_deploy_paths.add(f"systemd/{socket_unit}")
                matching_deploy_paths.add(f"systemd/{socket_stem}@.service")

    # Create renderer and temporarily change output dir to temp directory
    renderer = ScaffoldRenderer(config, server=server)

    # Save original output dir
    original_output_dir = renderer.output_dir

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        renderer.output_dir = temp_path

        try:
            generated_files = renderer.render(dry_run=False)
        finally:
            # Restore original output dir
            renderer.output_dir = original_output_dir

        # Get install mapping
        install_mapping = renderer.get_install_mapping()

        results: list[FileDiff] = []

        # Check each generated file
        for rel_path in generated_files:
            generated_file = temp_path / rel_path

            # Skip files that don't have install mappings
            if rel_path not in install_mapping:
                continue

            installed_path = install_mapping[rel_path]

            # Apply filters if specified
            if fraise_filter or env_filter:
                if not _file_matches_filters(rel_path, matching_deploy_paths):
                    continue

            # Compare files
            diff = _compare_files(generated_file, installed_path)
            results.append(diff)

        # Check for files that exist in install locations but not in scaffold
        for rel_path, installed_path in install_mapping.items():
            if installed_path.exists() and not (temp_path / rel_path).exists():
                # Apply filters
                if fraise_filter or env_filter:
                    if not _file_matches_filters(rel_path, matching_deploy_paths):
                        continue

                results.append(
                    FileDiff(
                        generated_path=rel_path,
                        installed_path=installed_path,
                        status="missing_generated",
                    )
                )

    return results


def _compare_files(generated_file: Path, installed_path: Path) -> FileDiff:
    """Compare a generated file against its installed version."""
    rel_path = str(generated_file.relative_to(generated_file.parent.parent))

    # Check if installed file exists
    if not installed_path.exists():
        return FileDiff(
            generated_path=rel_path,
            installed_path=installed_path,
            status="missing_installed",
        )

    # Read both files
    try:
        with generated_file.open(encoding="utf-8") as f:
            generated_content = f.readlines()

        with installed_path.open(encoding="utf-8") as f:
            installed_content = f.readlines()

        # Check if they match
        if generated_content == installed_content:
            return FileDiff(
                generated_path=rel_path,
                installed_path=installed_path,
                status="match",
            )
        else:
            # Generate unified diff
            diff_lines = list(
                difflib.unified_diff(
                    installed_content,
                    generated_content,
                    fromfile=str(installed_path),
                    tofile=str(generated_file),
                    lineterm="",
                )
            )

            return FileDiff(
                generated_path=rel_path,
                installed_path=installed_path,
                status="differs",
                diff_lines=diff_lines,
            )

    except (OSError, UnicodeDecodeError) as e:
        # If we can't read files, consider them different
        return FileDiff(
            generated_path=rel_path,
            installed_path=installed_path,
            status="differs",
            diff_lines=[f"Error reading files: {e}"],
        )


def _file_matches_filters(rel_path: str, matching_deploy_paths: set[str]) -> bool:
    """Check if a path matches the pre-computed set of allowed deploy unit paths."""
    if not matching_deploy_paths:
        return True

    # Deploy socket/service units are checked against the pre-computed set.
    # Other files (nginx, sudoers, timers, ...) are always included.
    if "systemd/" in rel_path and "deploy" in rel_path:
        return rel_path in matching_deploy_paths

    return True

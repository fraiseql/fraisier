"""Version management for Fraisier projects.

Manages version.json with semver versioning, schema hash tracking,
and database version derivation.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

_VERSION_FIELDS = frozenset(
    {
        "version",
        "commit",
        "branch",
        "timestamp",
        "environment",
        "schema_hash",
        "database_version",
    }
)


def is_valid_semver(version: str) -> bool:
    """Return True if *version* matches ``MAJOR.MINOR.PATCH``."""
    return _SEMVER_RE.match(version) is not None


def parse_semver(version: str) -> tuple[int, int, int]:
    """Parse a semver string into (major, minor, patch).

    Raises ValueError for invalid format.
    """
    m = _SEMVER_RE.match(version)
    if not m:
        raise ValueError(f"Invalid semver: {version!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


@dataclass
class VersionInfo:
    """Version metadata stored in version.json."""

    version: str = "0.0.0"
    commit: str = ""
    branch: str = ""
    timestamp: str = ""
    environment: str = ""
    schema_hash: str = ""
    database_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict (only non-empty fields)."""
        return {
            "version": self.version,
            "commit": self.commit,
            "branch": self.branch,
            "timestamp": self.timestamp,
            "environment": self.environment,
            "schema_hash": self.schema_hash,
            "database_version": self.database_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VersionInfo":
        """Deserialize from dict, ignoring unknown keys."""
        filtered = {k: v for k, v in data.items() if k in _VERSION_FIELDS}
        return cls(**filtered)


@dataclass
class VersionSyncTarget:
    """Configuration for syncing version to a target file."""

    path: Path
    regex: str

    def __post_init__(self) -> None:
        """Compile regex for efficiency."""
        self._compiled_regex = re.compile(self.regex, re.MULTILINE)


@dataclass
class VersionSyncConfig:
    """Configuration for version syncing to multiple files."""

    targets: list[VersionSyncTarget]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VersionSyncConfig":
        """Create from dict configuration."""
        targets = [
            VersionSyncTarget(
                path=Path(target_data["path"]), regex=target_data["regex"]
            )
            for target_data in data.get("sync_to", [])
        ]
        return cls(targets=targets)

    @classmethod
    def auto_discover(cls, root_path: Path) -> "VersionSyncConfig":
        """Auto-discover common version files to sync."""
        targets = []

        # pyproject.toml
        pyproject_path = root_path / "pyproject.toml"
        if pyproject_path.exists():
            targets.append(
                VersionSyncTarget(
                    path=pyproject_path, regex=r'^(version\s*=\s*")([^"]+)(")'
                )
            )

        # __init__.py files with __version__
        init_targets = [
            VersionSyncTarget(path=init_file, regex=r'^(__version__\s*=\s*")([^"]+)(")')
            for init_file in root_path.rglob("*/__init__.py")
            if init_file.read_text().find("__version__") != -1
        ]
        targets.extend(init_targets)

        return cls(targets=targets)


def write_version(info: VersionInfo, path: Path) -> None:
    """Write version info to a JSON file."""
    path.write_text(json.dumps(info.to_dict(), indent=2) + "\n")


def read_version(path: Path) -> VersionInfo | None:
    """Read version info from a JSON file. Returns None if missing."""
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return VersionInfo.from_dict(data)


def bump_version(
    pyproject_path: Path,
    part: str,
    sync_config: VersionSyncConfig | None = None,
) -> VersionInfo:
    """Atomically bump the version in *pyproject_path*.

    ``pyproject.toml`` is the single source of truth for the version string.
    Uses a temp-file + rename for atomicity: if any sync-target write fails,
    ``pyproject.toml`` is left unchanged.

    Args:
        pyproject_path: Path to pyproject.toml.
        part: "major", "minor", or "patch".
        sync_config: Optional configuration for syncing to additional files
            (e.g. ``__init__.py``).

    Returns:
        VersionInfo with the new version string (all other fields empty).

    Raises:
        FileNotFoundError: If pyproject.toml does not exist.
        ValueError: If *part* is invalid or version line is missing.
    """
    import os
    import tempfile

    if part not in ("major", "minor", "patch"):
        raise ValueError(f"Invalid bump part: {part!r}")

    current = read_pyproject_version(pyproject_path)
    major, minor, patch_v = parse_semver(current)

    if part == "major":
        major += 1
        minor = 0
        patch_v = 0
    elif part == "minor":
        minor += 1
        patch_v = 0
    else:
        patch_v += 1

    new_version = f"{major}.{minor}.{patch_v}"

    # Build the new pyproject.toml content via regex substitution.
    original_content = pyproject_path.read_text()
    new_content = _PYPROJECT_VERSION_RE.sub(
        rf"\g<1>{new_version}\g<3>", original_content
    )

    # Write to temp file first; sync additional targets before committing.
    # If any step fails, pyproject.toml stays unchanged.
    tmp_fd, tmp_name = tempfile.mkstemp(dir=pyproject_path.parent, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_text(new_content)

        if sync_config is not None:
            sync_version_to_targets(new_version, sync_config)

        # Atomic rename — commits pyproject.toml only after all targets succeed.
        tmp_path.rename(pyproject_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        os.close(tmp_fd)

    return VersionInfo(version=new_version)


_PYPROJECT_VERSION_RE = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.MULTILINE)


def sync_pyproject_version(version: str, pyproject_path: Path) -> None:
    """Update the ``version`` field in pyproject.toml.

    Raises FileNotFoundError if the file does not exist.
    """
    if not pyproject_path.exists():
        raise FileNotFoundError(f"pyproject.toml not found: {pyproject_path}")

    content = pyproject_path.read_text()
    new_content = _PYPROJECT_VERSION_RE.sub(rf"\g<1>{version}\g<3>", content)
    pyproject_path.write_text(new_content)


def sync_version_to_targets(version: str, sync_config: VersionSyncConfig) -> None:
    """Update version in all configured target files.

    Uses atomic writes - if any target fails, no targets are modified.

    Raises FileNotFoundError if any target file does not exist.
    """
    # First, validate all targets exist and create backups
    backups = []
    for target in sync_config.targets:
        if not target.path.exists():
            raise FileNotFoundError(f"Target file not found: {target.path}")

        # Create backup
        backup_path = target.path.with_suffix(target.path.suffix + ".bak")
        backup_path.write_text(target.path.read_text())
        backups.append((target.path, backup_path))

    try:
        # Update all targets
        for target in sync_config.targets:
            content = target.path.read_text()
            new_content = target._compiled_regex.sub(rf"\g<1>{version}\g<3>", content)
            target.path.write_text(new_content)
    except Exception:
        # Rollback all targets to backups
        for original_path, backup_path in backups:
            if backup_path.exists():
                original_path.write_text(backup_path.read_text())
        raise
    finally:
        # Clean up backups
        for _, backup_path in backups:
            backup_path.unlink(missing_ok=True)


def read_pyproject_version(pyproject_path: Path) -> str:
    """Read the version string from a pyproject.toml file.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If no ``version =`` line is found.
    """
    if not pyproject_path.exists():
        raise FileNotFoundError(f"pyproject.toml not found: {pyproject_path}")
    content = pyproject_path.read_text()
    m = _PYPROJECT_VERSION_RE.search(content)
    if not m:
        raise ValueError(f"No version field found in {pyproject_path}")
    return m.group(2)


def generate_version_json(
    app_path: Path,
    schema_dir: Path | None = None,
) -> "VersionInfo":
    """Build a VersionInfo from pyproject.toml + git metadata.

    Reads the version string from ``app_path/pyproject.toml``, queries git
    for commit SHA and branch, and optionally computes schema_hash /
    database_version from SQL files in *schema_dir*.

    This function does **not** write any file — the caller is responsible
    for calling ``write_version(info, dest_path)``.

    Args:
        app_path: Root directory of the managed project (must contain pyproject.toml).
        schema_dir: Optional path to a directory containing ``*.sql`` migration files.

    Returns:
        VersionInfo populated with version, commit, branch, timestamp, and
        optionally schema_hash / database_version.
    """
    import subprocess
    from datetime import UTC, datetime

    version = read_pyproject_version(app_path / "pyproject.toml")

    def _git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(app_path), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    commit = _git("rev-parse", "--short", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    timestamp = datetime.now(tz=UTC).isoformat()

    schema_hash = ""
    database_version = ""
    if schema_dir is not None:
        from fraisier.dbops.schema import _compute_schema_hash

        schema_hash = f"sha256:{_compute_schema_hash(schema_dir)}"
        sql_count = len(list(schema_dir.glob("*.sql")))
        database_version = derive_database_version(sequence=sql_count)

    return VersionInfo(
        version=version,
        commit=commit,
        branch=branch,
        timestamp=timestamp,
        schema_hash=schema_hash,
        database_version=database_version,
    )


def derive_database_version(*, sequence: int) -> str:
    """Derive a database version string in ``YYYY.MM.DD.NNN`` format."""
    from datetime import UTC, datetime

    now = datetime.now(tz=UTC)
    return f"{now.year}.{now.month:02d}.{now.day:02d}.{sequence:03d}"


def has_version_changed(
    old_version: str | None,
    new_version: str,
) -> bool:
    """Return True if version has changed (or old is None)."""
    if old_version is None:
        return True
    return old_version != new_version

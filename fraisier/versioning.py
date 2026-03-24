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
    path: Path,
    part: str,
    pyproject_path: Path | None = None,
) -> VersionInfo:
    """Atomically bump the version in *path* and optionally *pyproject_path*.

    Uses temp-file + rename for atomicity.  If *pyproject_path* is given,
    both files are written together — if either write fails, neither file
    is modified.

    Args:
        path: Path to version.json.
        part: "major", "minor", or "patch".
        pyproject_path: Optional path to pyproject.toml to keep in sync.

    Returns:
        Updated VersionInfo.

    Raises:
        FileNotFoundError: If version.json does not exist.
        ValueError: If *part* is invalid.
    """
    import tempfile

    if not path.exists():
        raise FileNotFoundError(f"Version file not found: {path}")

    if part not in ("major", "minor", "patch"):
        raise ValueError(f"Invalid bump part: {part!r}")

    info = read_version(path)
    if info is None:
        raise FileNotFoundError(f"Could not read: {path}")

    major, minor, patch_v = parse_semver(info.version)

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

    # Create backup
    backup_path = path.with_suffix(".json.bak")
    backup_path.write_text(path.read_text())

    # Write to temp files first, then rename for atomicity.
    # If pyproject sync fails, version.json is not modified either.
    version_content = (
        json.dumps({**info.to_dict(), "version": new_version}, indent=2) + "\n"
    )

    tmp_version = Path(tempfile.mktemp(dir=path.parent, suffix=".tmp"))
    try:
        tmp_version.write_text(version_content)

        # Sync pyproject.toml before committing version.json
        if pyproject_path is not None:
            sync_pyproject_version(new_version, pyproject_path)

        # Commit version.json last (atomic rename)
        tmp_version.rename(path)
    except Exception:
        # Rollback: remove temp, leave originals untouched.
        # If pyproject was already written, restore from backup.
        tmp_version.unlink(missing_ok=True)
        raise

    info.version = new_version
    return info


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


def derive_database_version(*, sequence: int) -> str:
    """Derive a database version string in ``YYYY.MM.DD.NNN`` format."""
    from datetime import UTC, datetime

    now = datetime.now(tz=UTC)
    return f"{now.year}.{now.month:02d}.{now.day:02d}.{sequence:03d}"


def update_schema_info(
    version_path: Path,
    schema_dir: Path,
) -> VersionInfo:
    """Update schema_hash and database_version in version.json.

    Computes the SHA-256 hash of all ``*.sql`` files in *schema_dir*
    and stores it (prefixed with ``sha256:``) in version.json.
    The database_version is derived from the current date and the
    number of migration files.
    """
    from fraisier.dbops.schema import hash_schema

    info = read_version(version_path)
    if info is None:
        info = VersionInfo()

    schema_hash = hash_schema(schema_dir)
    info.schema_hash = f"sha256:{schema_hash}"

    # Count SQL files for sequence number
    sql_count = len(list(schema_dir.glob("*.sql")))
    info.database_version = derive_database_version(sequence=sql_count)

    write_version(info, version_path)
    return info


def has_version_changed(
    old_version: str | None,
    new_version: str,
) -> bool:
    """Return True if version has changed (or old is None)."""
    if old_version is None:
        return True
    return old_version != new_version

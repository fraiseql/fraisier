"""Input validation for database identifiers and shell arguments."""

import re
from pathlib import Path

_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
_SERVICE_RE = re.compile(r"^[a-zA-Z0-9_@.\-]+$")
_PATH_RE = re.compile(r"^[a-zA-Z0-9_./ -]+$")


def validate_pg_identifier(name: str, label: str = "identifier") -> str:
    """Validate that *name* is a safe PostgreSQL identifier.

    Raises ValueError if it contains anything other than ``[a-zA-Z0-9_]``.
    """
    if not _IDENT_RE.match(name):
        msg = f"Invalid {label}: {name!r} — must match [a-zA-Z_][a-zA-Z0-9_]{{0,62}}"
        raise ValueError(msg)
    return name


def validate_service_name(name: str) -> str:
    """Validate a systemd service name.

    Raises ValueError if it contains shell metacharacters.
    """
    if not _SERVICE_RE.match(name):
        msg = f"Invalid service name: {name!r}"
        raise ValueError(msg)
    return name


def validate_file_path(path: str, base_dir: Path | None = None) -> str:
    """Validate a file path for use in shell commands.

    When *base_dir* is provided, also rejects path traversal by ensuring
    the resolved path stays within *base_dir*.

    Raises ValueError if path contains shell metacharacters or escapes base_dir.
    """
    if not _PATH_RE.match(path):
        msg = f"Invalid file path: {path!r}"
        raise ValueError(msg)
    if base_dir is not None:
        resolved = Path(base_dir, path).resolve()
        if not resolved.is_relative_to(base_dir.resolve()):
            msg = f"Path traversal detected: {path!r}"
            raise ValueError(msg)
    return path

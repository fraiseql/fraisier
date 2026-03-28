"""Input validation for database identifiers and shell arguments."""

import re
import shlex
from pathlib import Path

_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
_SERVICE_RE = re.compile(r"^[a-zA-Z0-9_@.\-]+$")
_PATH_RE = re.compile(r"^[a-zA-Z0-9_./ -]+$")

_SHELL_METACHARACTERS = re.compile(r"[;|&`$()]")


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


def validate_docker_cp_path(cp_path: str) -> str:
    """Validate a docker cp path (container:path).

    Rejects path traversal via ``..`` components and relative paths.
    Raises ValueError if invalid.
    """
    if ":" not in cp_path:
        msg = f"Invalid docker cp path (missing ':'): {cp_path!r}"
        raise ValueError(msg)
    container, path = cp_path.split(":", 1)
    if not container:
        msg = f"Invalid docker cp path (empty container): {cp_path!r}"
        raise ValueError(msg)
    if ".." in path:
        msg = f"Path traversal detected in docker cp path: {cp_path!r}"
        raise ValueError(msg)
    if not path.startswith("/"):
        msg = f"Docker cp path must start with /: {cp_path!r}"
        raise ValueError(msg)
    return cp_path


def validate_file_path(
    path: str,
    base_dir: Path | None = None,
    *,
    strict: bool = False,
) -> str:
    """Validate a file path for use in shell commands.

    When *base_dir* is provided, also rejects path traversal by ensuring
    the resolved path stays within *base_dir*.

    When *strict* is True, rejects any symlink in the path.

    Raises ValueError if path contains shell metacharacters or escapes base_dir.
    """
    if not _PATH_RE.match(path):
        msg = f"Invalid file path: {path!r}"
        raise ValueError(msg)
    if ".." in Path(path).parts:
        msg = f"Path traversal detected: {path!r}"
        raise ValueError(msg)
    if base_dir is not None:
        resolved = Path(base_dir, path).resolve()
        if not resolved.is_relative_to(base_dir.resolve()):
            msg = f"Path traversal detected: {path!r}"
            raise ValueError(msg)
    p = Path(path)
    if strict and p.exists() and p.is_symlink():
        msg = f"Symlink not allowed in strict mode: {path!r}"
        raise ValueError(msg)
    return path


def validate_shell_command(
    cmd: str,
    allowed_binaries: set[str] | None = None,
) -> list[str]:
    """Validate a shell command string for safe execution.

    Rejects commands containing shell metacharacters (;, |, &, `, $, (, )).
    Optionally validates the binary against an allowlist.

    Returns the parsed command as a list of arguments.
    Raises ValueError on invalid input.
    """
    if _SHELL_METACHARACTERS.search(cmd):
        msg = (
            f"Shell metacharacter detected in command: {cmd!r}. "
            "Commands must not contain ;, |, &, `, $, (, or )."
        )
        raise ValueError(msg)

    tokens = shlex.split(cmd)
    if not tokens:
        msg = "Empty command"
        raise ValueError(msg)

    binary = Path(tokens[0]).name
    if allowed_binaries is not None and binary not in allowed_binaries:
        msg = (
            f"Binary {binary!r} not in allowed list: "
            f"{', '.join(sorted(allowed_binaries))}"
        )
        raise ValueError(msg)

    return tokens

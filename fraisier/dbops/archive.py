"""Is this file a readable pg_dump archive? (#342, #343)

One seam, three consumers: the restore path checks before it drops a database,
``backup prune`` checks before it spends a ``keep_minimum`` slot, and ``doctor``
reports what it finds. "What makes a dump valid" is the kind of fact this
codebase keeps having to stop deriving in a second place — see #337 for unit
names and #283 for server-side paths — so it is answered here and nowhere else.

The answer is **three-valued**. A host whose only job is to hold dumps may have
no PostgreSQL client tools, and "I could not check" is not "the dump is bad":
one caller deletes files on that reasoning and another refuses to restore.
:attr:`ArchiveCheck.is_bad` is true for :attr:`ArchiveVerdict.INVALID` alone, so
the safe reading is also the short one. This is the same rule ``db restore``
already applies to its deployment lock, where a lock that cannot be evaluated is
an error rather than a skip: an unevaluable condition is never silently resolved
in either direction.

Why ``pg_restore --list`` and not the file header: a dump truncated mid-transfer
still carries the ``PGDMP`` magic, so reading five bytes proves nothing. ``--list``
parses the table of contents, needs no database connection, and fails in under a
second on the corpus that matters.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# pg_restore --list reads the archive only. A minute is far longer than the
# parse needs and short enough that a hung call cannot stall a nightly prune.
_LIST_TIMEOUT_SECONDS = 60


class ArchiveVerdict(Enum):
    """What was learned about an archive."""

    VALID = "valid"
    """``pg_restore --list`` read the table of contents."""

    INVALID = "invalid"
    """``pg_restore --list`` rejected the file. It is not a usable archive."""

    UNVERIFIABLE = "unverifiable"
    """The check could not run: no ``pg_restore``, no file, or it timed out.

    Says nothing about the archive. Callers must not act as though it does.
    """


@dataclass(frozen=True)
class ArchiveCheck:
    """A verdict and why it was reached."""

    verdict: ArchiveVerdict
    detail: str

    @property
    def is_valid(self) -> bool:
        """The archive was read successfully."""
        return self.verdict is ArchiveVerdict.VALID

    @property
    def is_bad(self) -> bool:
        """The archive is known to be unusable.

        ``UNVERIFIABLE`` is **not** bad. Branch on this rather than on
        ``verdict != VALID``, which convicts a dump for the absence of the tool
        that would have cleared it.
        """
        return self.verdict is ArchiveVerdict.INVALID


def _list_command(path: Path) -> list[str]:
    """The argv that reads *path*'s table of contents.

    Extracted so a test can assert the command without running it — and so the
    absence of connection flags is visible as a property of one function.
    """
    return ["pg_restore", "--list", str(path)]


def verify_archive(path: Path | str) -> ArchiveCheck:
    """Report whether *path* is an archive ``pg_restore`` can read.

    Args:
        path: A ``-Fc`` dump file or a ``-Fd`` dump directory.

    Returns:
        An :class:`ArchiveCheck`. Never raises for an unreadable path or a
        missing ``pg_restore`` — those are ``UNVERIFIABLE`` results, because a
        caller deciding what to delete needs a verdict rather than an exception
        it is likely to convert into one.
    """
    target = Path(path)
    if not target.exists():
        return ArchiveCheck(
            ArchiveVerdict.UNVERIFIABLE, f"{target} does not exist — nothing to verify"
        )

    try:
        result = subprocess.run(
            _list_command(target),
            capture_output=True,
            text=True,
            check=False,
            timeout=_LIST_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return ArchiveCheck(
            ArchiveVerdict.UNVERIFIABLE,
            "pg_restore not found on PATH — install the PostgreSQL client "
            "tools to verify dumps on this host",
        )
    except subprocess.TimeoutExpired:
        return ArchiveCheck(
            ArchiveVerdict.UNVERIFIABLE,
            f"pg_restore --list on {target} did not finish within "
            f"{_LIST_TIMEOUT_SECONDS}s",
        )

    if result.returncode != 0:
        return ArchiveCheck(
            ArchiveVerdict.INVALID,
            (result.stderr or result.stdout or "").strip()
            or f"pg_restore --list exited with code {result.returncode}",
        )

    return ArchiveCheck(ArchiveVerdict.VALID, "")

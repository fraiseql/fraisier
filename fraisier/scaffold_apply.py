"""Apply a deploy's render as root, inside the bounds of the root policy (#433).

This replaces running the rendered ``install.sh`` as root, which made the
deploy user — and anyone who can land a commit on an auto-deployed branch —
root: the deploy user writes that script.

The render is read as untrusted input. The root policy says which units a
deploy may rewrite and where each one's content comes from; the content must
then pass :func:`fraisier.unit_validator.validate_unit`. Everything else root
owns (sudoers, nginx, sockets, the root helpers) is only compared: if the
render wants it changed, the apply stops and names it, because only an operator
may change it, with ``sudo fraisier scaffold-install``.

Nothing is written unless everything passes. A host left half on one render
and half on another is the worst state to recover from.
"""

from __future__ import annotations

import errno
import json
import os
import posixpath
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from fraisier.root_policy import (
    ACTION_INSTALL_HELPER,
    ACTION_TIMER,
    ACTION_WEBHOOK,
    RootPolicy,
)
from fraisier.scaffold.artifacts import (
    ARTIFACT_MANIFEST_NAME,
    SYSTEMD_DIR,
    host_artifacts,
)
from fraisier.unit_validator import Refusal, check_scaffold_unit_name, validate_unit

if TYPE_CHECKING:
    from collections.abc import Callable


#: A unit file is a few KiB; anything bigger is not one.
MAX_TREE_FILE_BYTES = 1024 * 1024

RULE_TREE = "tree"


class UnsafeTreeError(Exception):
    """A tree entry root must not read: a link, a non-file, or a path escape."""


@dataclass
class ApplyOutcome:
    installed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    refused: dict[str, list[Refusal]] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    """What only an operator may change, and why it is waiting."""

    deferred_restarts: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.refused or self.pending or self.failed)

    def report(self) -> str:
        """Every reason this apply did not complete, one per line."""
        lines = [
            f"refused {unit}: {refusal}"
            for unit, refusals in sorted(self.refused.items())
            for refusal in refusals
        ]
        lines += [f"pending operator install: {item}" for item in self.pending]
        lines += [f"failed: {item}" for item in self.failed]
        if self.pending:
            lines.append(
                "An operator must approve this change: run `sudo fraisier "
                "scaffold-install` against the deployed commit's fraises.yaml."
            )
        return "\n".join(lines)


def _known_destinations(policy: RootPolicy) -> set[str]:
    return {f"{SYSTEMD_DIR}/{name}" for name in policy.units} | set(
        policy.operator_only
    )


def _new_artifacts(
    policy: RootPolicy, read_tree: Callable[[str], bytes | None], hostname: str
) -> list[str]:
    """Artifacts the render wants on this host that no operator approved."""
    try:
        raw = read_tree(ARTIFACT_MANIFEST_NAME)
    except UnsafeTreeError as exc:
        return [f"{ARTIFACT_MANIFEST_NAME}: {exc}"]
    if raw is None:
        return [f"{ARTIFACT_MANIFEST_NAME}: not in the render"]
    try:
        payload = json.loads(raw)
    except UnicodeDecodeError, json.JSONDecodeError:
        return [f"{ARTIFACT_MANIFEST_NAME}: not valid JSON"]
    if not isinstance(payload, dict):
        return [f"{ARTIFACT_MANIFEST_NAME}: not a manifest"]
    chosen = host_artifacts(payload, hostname)
    if chosen is None:
        return [
            f"{ARTIFACT_MANIFEST_NAME}: this host ({hostname}) is not in the render"
        ]
    known = _known_destinations(policy)
    return [
        f"{dest}: new, never approved by an operator"
        for dest in sorted({str(a.get("destination")) for a in chosen} - known)
    ]


def apply_scaffold(
    policy: RootPolicy,
    *,
    read_tree: Callable[[str], bytes | None],
    read_installed: Callable[[str], bytes | None],
    write_unit: Callable[[str, bytes], None],
    systemctl: Callable[..., bool],
    hostname: str,
    deploy_in_flight: bool,
) -> ApplyOutcome:
    """Bring this host's units in line with the render, or say why not."""
    outcome = ApplyOutcome()
    to_write: dict[str, bytes] = {}

    for name, grant in sorted(policy.units.items()):
        name_refusal = check_scaffold_unit_name(name, policy)
        if name_refusal is not None:
            outcome.refused[name] = [name_refusal]
            continue
        try:
            data = read_tree(grant.source)
        except UnsafeTreeError as exc:
            outcome.refused[name] = [Refusal(RULE_TREE, str(exc))]
            continue
        if data is None:
            outcome.pending.append(f"{name}: {grant.source} is not in the render")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            outcome.refused[name] = [Refusal("syntax", "the file is not UTF-8")]
            continue
        refusals = validate_unit(name, text, policy)
        if refusals:
            outcome.refused[name] = refusals
        elif read_installed(f"{SYSTEMD_DIR}/{name}") == data:
            outcome.unchanged.append(name)
        else:
            to_write[name] = data

    for dest, source in sorted(policy.operator_only.items()):
        try:
            rendered = read_tree(source) if source else None
        except UnsafeTreeError as exc:
            outcome.refused[dest] = [Refusal(RULE_TREE, str(exc))]
            continue
        if rendered is None:
            continue
        installed = read_installed(dest)
        if installed is None:
            outcome.pending.append(f"{dest}: not installed")
        elif installed != rendered:
            outcome.pending.append(f"{dest}: differs from the render")

    outcome.pending += _new_artifacts(policy, read_tree, hostname)

    if not outcome.ok:
        return outcome

    for name, data in to_write.items():
        write_unit(name, data)
        outcome.installed.append(name)
    if not to_write:
        return outcome
    if not systemctl("daemon-reload"):
        outcome.failed.append("systemctl daemon-reload")
        return outcome

    for name in outcome.installed:
        action = policy.units[name].action
        if action == ACTION_TIMER and name.endswith(".timer"):
            args: tuple[str, ...] = ("enable", "--now", name)
        elif action == ACTION_WEBHOOK:
            if deploy_in_flight:
                # Restarting the unit a deploy runs inside kills that deploy
                # (#349). The deploy records the debt and pays it once it has
                # released its lock.
                outcome.deferred_restarts.append(name)
                continue
            args = ("restart", name)
        elif action == ACTION_INSTALL_HELPER and name.endswith(".service"):
            # The running instance holds the old allowlist in its argv (#279);
            # stopped, its socket re-execs it from the new unit.
            args = ("stop", name)
        else:
            continue
        if not systemctl(*args):
            outcome.failed.append(f"systemctl {' '.join(args)}")
    return outcome


# ---------------------------------------------------------------------------
# I/O: the root helper's real collaborators
# ---------------------------------------------------------------------------


def _split_rel(rel: str) -> list[str]:
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(p in {"", ".", ".."} for p in parts):
        raise UnsafeTreeError(f"{rel!r} is not a path inside the scaffold tree")
    return parts


class SafeTree:
    """Read files from a tree someone else owns, without following their links.

    Every component from ``/`` to the file is opened with ``O_NOFOLLOW``, so a
    symlink anywhere — above the tree included — is refused rather than
    followed into a file root can read and the owner cannot. A hard link is
    refused too: it would hand root someone else's file under a tree name.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _open_dir(self) -> int:
        if not self.root.is_absolute():
            raise UnsafeTreeError(f"{self.root} is not absolute")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.root.parts[1:]:
                nxt = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = nxt
        except OSError as exc:
            os.close(fd)
            raise UnsafeTreeError(f"cannot open {self.root} safely: {exc}") from exc
        return fd

    def read(self, rel: str) -> bytes | None:
        parts = _split_rel(rel)
        fd = self._open_dir()
        try:
            for part in parts[:-1]:
                try:
                    nxt = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                except FileNotFoundError:
                    return None
                except OSError as exc:
                    raise UnsafeTreeError(f"{rel}: {exc.strerror}") from exc
                os.close(fd)
                fd = nxt
            try:
                file_fd = os.open(
                    parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise UnsafeTreeError(f"{rel} is a symlink") from exc
                raise UnsafeTreeError(f"{rel}: {exc.strerror}") from exc
            try:
                st = os.fstat(file_fd)
                if not stat.S_ISREG(st.st_mode):
                    raise UnsafeTreeError(f"{rel} is not a regular file")
                if st.st_nlink != 1:
                    raise UnsafeTreeError(f"{rel} has {st.st_nlink} hard links")
                chunks = []
                remaining = MAX_TREE_FILE_BYTES + 1
                while remaining > 0:
                    chunk = os.read(file_fd, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                if remaining <= 0:
                    raise UnsafeTreeError(f"{rel} is larger than a unit file can be")
                return b"".join(chunks)
            finally:
                os.close(file_fd)
        finally:
            os.close(fd)


def read_installed(path: str) -> bytes | None:
    """An installed root-owned file, or None. Anything not a file reads as b""."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


class UnitWriter:
    """Write unit files into a systemd directory atomically, mode 0644."""

    def __init__(self, directory: str | Path = SYSTEMD_DIR) -> None:
        self.directory = Path(directory)

    def write(self, name: str, data: bytes) -> None:
        if (
            not name
            or name.startswith(".")
            or posixpath.basename(name) != name
            or name in {".", ".."}
        ):
            raise ValueError(f"{name!r} is not a plain unit name")
        dir_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        tmp = f".{name}.fraisier-new"
        try:
            with suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=dir_fd)
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=dir_fd,
            )
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fchmod(fd, 0o644)
                os.fsync(fd)
            finally:
                os.close(fd)
            # rename replaces whatever is at *name*, a symlink included, without
            # following it.
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

"""Build the root policy from the render an operator just approved (#433).

Runs only inside an operator's ``sudo fraisier scaffold-install``, after the
operator has seen the diff of every root-owned file. What the policy grants is
read off that render — the identities, executables, files and directories its
units name — so the next deploy may change a unit's *content* within those
bounds, and anything wider waits for the operator again.

Two lines this never crosses, whatever the render says. An identity that
resolves to uid or gid 0 is not granted. A file root must never read on a
unit's behalf (``/etc/shadow``, sudoers, ``/root``) is not granted. A unit that
would still run something as root is not granted to a deploy at all; it is
listed as operator-only.
"""

from __future__ import annotations

import grp
import posixpath
import pwd
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from fraisier.root_policy import (
    ACTION_INSTALL_HELPER,
    ACTION_PLAIN,
    ACTION_TIMER,
    ACTION_WEBHOOK,
    RootPolicy,
    UnitGrant,
)
from fraisier.scaffold.artifacts import SYSTEMD_DIR, Disposition, host_artifacts
from fraisier.unit_validator import (
    DIRECTORY_KEYS,
    EXEC_KEYS,
    UNIT_SUFFIXES,
    check_app_unit_name,
    exec_executable,
    unit_directives,
    validate_unit,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping


#: The dispositions whose units a deploy may rewrite, and what follows a write.
_GRANTABLE: dict[str, str] = {
    Disposition.PLAIN: ACTION_PLAIN,
    Disposition.TIMER: ACTION_TIMER,
    Disposition.WEBHOOK: ACTION_WEBHOOK,
    Disposition.HELPER_REBAKE: ACTION_INSTALL_HELPER,
}

#: Files systemd must never read as root for a unit, whatever a render names.
_NEVER_READABLE = (
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/ssh/",
    "/root/",
)


class PolicyBuildError(Exception):
    """The render cannot be turned into a policy for this host."""


@dataclass(frozen=True)
class PolicyDraft:
    policy: RootPolicy
    notes: tuple[str, ...]
    """What the operator should know: units kept from deploys, and why."""


def _uid(name: str) -> int | None:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def _gid(name: str) -> int | None:
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


def _privileged(name: str, lookup: Callable[[str], int | None]) -> bool:
    if not name or name == "root" or (name.isdigit() and int(name) == 0):
        return True
    return lookup(name) == 0


@dataclass
class _Observed:
    users: set[str]
    groups: set[str]
    exec_prefixes: set[str]
    read_paths: set[str]
    directories: set[str]


def _observe(
    texts: Iterable[str],
    trees: tuple[str, ...],
    uid_of: Callable[[str], int | None],
    gid_of: Callable[[str], int | None],
) -> _Observed:
    seen = _Observed(set(), set(), set(trees), set(), set())
    for text in texts:
        for section, key, value in unit_directives(text):
            if section != "Service":
                continue
            if key == "User" and not _privileged(value, uid_of):
                seen.users.add(value)
            elif key == "Group" and not _privileged(value, gid_of):
                seen.groups.add(value)
            elif key in EXEC_KEYS:
                exe = exec_executable(value)
                if (
                    exe
                    and posixpath.isabs(exe)
                    and posixpath.normpath(exe) == exe
                    and not any(exe.startswith(tree) for tree in trees)
                ):
                    seen.exec_prefixes.add(exe)
            elif key in {"EnvironmentFile", "LoadCredential"}:
                path = (
                    value.removeprefix("-")
                    if key == "EnvironmentFile"
                    else value.partition(":")[2]
                )
                if posixpath.isabs(path) and not path.startswith(_NEVER_READABLE):
                    seen.read_paths.add(path)
            elif key in DIRECTORY_KEYS:
                seen.directories.update(value.split())
    return seen


def build_policy(
    *,
    project: str,
    scaffold_dir: str,
    payload: Mapping[str, Any],
    hostname: str,
    read_source: Callable[[str], str | None],
    app_units: Mapping[str, str],
    trees: Iterable[str],
    uid_of: Callable[[str], int | None] = _uid,
    gid_of: Callable[[str], int | None] = _gid,
) -> PolicyDraft:
    """The policy for *hostname*, from the render in *payload*.

    Args:
        payload: The render's ``artifact-manifest.json``, parsed.
        read_source: The rendered file at a manifest ``source``, or None.
        app_units: Unit name → text, for the app's own units this host's
            unit-installer copies (``fraisier scheduled-install``).
        trees: Directories a unit may run anything under — the deploy user's
            tool bin, each local ``app_path``, the scaffold dir. Each ends ``/``.
    """
    chosen = host_artifacts(payload, hostname)
    if chosen is None:
        raise PolicyBuildError(
            f"{hostname!r} is not a machine any server in this config lists"
        )

    notes: list[str] = []
    operator_only: dict[str, str] = {}
    candidates: dict[str, tuple[UnitGrant, str]] = {}
    for artifact in chosen:
        dest, source = artifact["destination"], artifact["source"]
        name = posixpath.basename(dest)
        action = _GRANTABLE.get(artifact.get("disposition", ""))
        if (
            action is None
            or posixpath.dirname(dest) != SYSTEMD_DIR
            or not name.endswith(UNIT_SUFFIXES)
        ):
            operator_only[dest] = source
            continue
        text = read_source(source)
        if text is None:
            operator_only[dest] = source
            notes.append(f"{name}: not in the render, so a deploy may not install it")
            continue
        candidates[name] = (UnitGrant(source=source, action=action), text)

    tree_prefixes = tuple(t if t.endswith("/") else f"{t}/" for t in trees)
    seen = _observe(
        [text for _grant, text in candidates.values()] + list(app_units.values()),
        tree_prefixes,
        uid_of,
        gid_of,
    )
    policy = RootPolicy(
        project=project,
        scaffold_dir=scaffold_dir,
        users=frozenset(seen.users),
        groups=frozenset(seen.groups),
        exec_prefixes=tuple(sorted(seen.exec_prefixes)),
        read_paths=frozenset(seen.read_paths),
        directories=frozenset(seen.directories),
    )

    granted: dict[str, UnitGrant] = {}
    for name, (grant, text) in sorted(candidates.items()):
        refusals = validate_unit(name, text, policy)
        if refusals:
            operator_only[f"{SYSTEMD_DIR}/{name}"] = grant.source
            reasons = "; ".join(str(r) for r in refusals)
            notes.append(f"{name}: only an operator may change it ({reasons})")
            continue
        granted[name] = grant

    policy = replace(
        policy, units=granted, operator_only=dict(sorted(operator_only.items()))
    )
    for name, text in sorted(app_units.items()):
        refusals = [
            r
            for r in (
                check_app_unit_name(name, policy),
                *validate_unit(name, text, policy),
            )
            if r is not None
        ]
        if refusals:
            reasons = "; ".join(str(r) for r in refusals)
            notes.append(f"{name}: the unit-installer will refuse it ({reasons})")
    return PolicyDraft(policy=policy, notes=tuple(notes))

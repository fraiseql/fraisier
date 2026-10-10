"""The root-owned policy that bounds what a deploy may install as root (#433).

A deploy renders units as the deploy user, and anyone who can land a commit on
an auto-deployed branch chooses what that render contains: each deploy copies
the deployed commit's ``fraises.yaml`` over ``/opt/fraisier``. So nothing the
deploy user can write may decide what root accepts. This file is that anchor.
It lives at ``/etc/fraisier/<project>/root-policy.json``, is written only by an
operator's ``sudo fraisier scaffold-install``, and is refused unless it and
every directory above it are root-owned and writable by root alone.

It records what the operator approved in that run: the units a deploy may
update in place (and the source each comes from), the identities they may run
as, the executables and files they may name, and the root-owned files a deploy
may *not* change — sudoers, nginx, sockets and the root helpers themselves —
whose drift stops the deploy until the operator runs scaffold-install again.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

#: Root-owned, outside everything the deploy user owns (``/opt/fraisier``,
#: ``/var/lib/fraisier``).
POLICY_ROOT = Path("/etc/fraisier")
POLICY_NAME = "root-policy.json"
POLICY_SCHEMA_VERSION = 1

#: What a granted unit needs after its file is written.
ACTION_PLAIN = "plain"
ACTION_TIMER = "timer"
ACTION_WEBHOOK = "webhook"
ACTION_INSTALL_HELPER = "install_helper"
ACTIONS = frozenset({ACTION_PLAIN, ACTION_TIMER, ACTION_WEBHOOK, ACTION_INSTALL_HELPER})


class RootPolicyError(Exception):
    """The policy is missing, unsafe to trust, or malformed."""


@dataclass(frozen=True)
class UnitGrant:
    """A unit a deploy may rewrite, and where its new content comes from."""

    source: str
    """Path relative to the scaffold dir."""

    action: str
    """One of ``ACTIONS``."""


@dataclass(frozen=True)
class RootPolicy:
    """What the operator approved. Everything a deploy installs must fit it."""

    project: str
    scaffold_dir: str
    users: frozenset[str]
    groups: frozenset[str]
    exec_prefixes: tuple[str, ...]
    """A trailing ``/`` grants a directory tree; anything else is one file."""

    read_paths: frozenset[str]
    """Files systemd may read as root for a unit (``EnvironmentFile=``,
    ``LoadCredential=``). Exact paths: a directory would grant its siblings."""

    directories: frozenset[str]
    """Names systemd may create and chown under ``/run``, ``/var/lib``,
    ``/var/log``, ``/var/cache`` and ``/etc`` (``RuntimeDirectory=`` …)."""

    units: Mapping[str, UnitGrant] = field(default_factory=dict)
    """Unit name under ``/etc/systemd/system`` → its grant."""

    operator_only: Mapping[str, str] = field(default_factory=dict)
    """Absolute destination → scaffold source, for files only an operator may
    change. A source of ``""`` means "watch for drift, nothing renders it"."""


def policy_path(project: str, root: Path = POLICY_ROOT) -> Path:
    return root / project / POLICY_NAME


def dump_policy(policy: RootPolicy) -> str:
    payload = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "project": policy.project,
        "scaffold_dir": policy.scaffold_dir,
        "users": sorted(policy.users),
        "groups": sorted(policy.groups),
        "exec_prefixes": sorted(policy.exec_prefixes),
        "read_paths": sorted(policy.read_paths),
        "directories": sorted(policy.directories),
        "units": {
            name: {"source": g.source, "action": g.action}
            for name, g in sorted(policy.units.items())
        },
        "operator_only": dict(sorted(policy.operator_only.items())),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _strings(payload: dict, key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RootPolicyError(f"policy key {key!r} must be a list of strings")
    return value


def parse_policy(raw: str) -> RootPolicy:
    """Parse a policy file's text. Raises ``RootPolicyError`` on any doubt."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RootPolicyError(f"policy is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RootPolicyError("policy must be a JSON object")
    if payload.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise RootPolicyError(
            f"policy schema_version {payload.get('schema_version')!r} is not "
            f"{POLICY_SCHEMA_VERSION}"
        )
    for key in ("project", "scaffold_dir"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise RootPolicyError(f"policy key {key!r} must be a non-empty string")
    units_raw = payload.get("units")
    operator_raw = payload.get("operator_only")
    if not isinstance(units_raw, dict) or not isinstance(operator_raw, dict):
        raise RootPolicyError("policy 'units' and 'operator_only' must be objects")
    units: dict[str, UnitGrant] = {}
    for name, grant in units_raw.items():
        if (
            not isinstance(grant, dict)
            or not isinstance(grant.get("source"), str)
            or grant.get("action") not in ACTIONS
        ):
            raise RootPolicyError(f"policy unit {name!r} is malformed")
        units[name] = UnitGrant(source=grant["source"], action=grant["action"])
    if not all(
        isinstance(k, str) and isinstance(v, str) for k, v in operator_raw.items()
    ):
        raise RootPolicyError("policy 'operator_only' must map strings to strings")
    return RootPolicy(
        project=payload["project"],
        scaffold_dir=payload["scaffold_dir"],
        users=frozenset(_strings(payload, "users")),
        groups=frozenset(_strings(payload, "groups")),
        exec_prefixes=tuple(_strings(payload, "exec_prefixes")),
        read_paths=frozenset(_strings(payload, "read_paths")),
        directories=frozenset(_strings(payload, "directories")),
        units=units,
        operator_only=dict(operator_raw),
    )


def _root_only(st: os.stat_result) -> bool:
    """Owned by root, and writable by nobody else (gid 0 may share it).

    A sticky directory root owns (``/tmp``) passes: others may add entries to
    it but cannot rename or remove root's. The same rule as doctor's
    ``root_unit_exec_trust``.
    """
    if st.st_uid != 0:
        return False
    if stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX:
        return True
    if st.st_mode & stat.S_IWOTH:
        return False
    return not (st.st_mode & stat.S_IWGRP and st.st_gid != 0)


def load_policy(
    path: Path,
    *,
    lstat: Callable[[Path], os.stat_result] = os.lstat,
    read_text: Callable[[Path], str] = Path.read_text,
) -> RootPolicy:
    """Read the policy at *path*, refusing it unless only root could have written it.

    Every component from ``/`` down must be a real directory (or, last, a
    regular file) that root owns and nobody else can write. A symlink
    anywhere is refused rather than followed: whoever can re-point it chooses
    the policy.
    """
    path = Path(path)
    if not path.is_absolute():
        raise RootPolicyError(f"policy path {path} is not absolute")
    parts = [Path(*path.parts[: i + 1]) for i in range(len(path.parts))]
    for component in parts:
        try:
            st = lstat(component)
        except FileNotFoundError as exc:
            raise RootPolicyError(
                f"no root policy at {path}: run `sudo fraisier scaffold-install` "
                "as an operator to write it"
            ) from exc
        except OSError as exc:
            raise RootPolicyError(f"cannot stat {component}: {exc}") from exc
        last = component == path
        if stat.S_ISLNK(st.st_mode):
            raise RootPolicyError(f"{component} is a symlink; refusing the policy")
        if not (stat.S_ISREG(st.st_mode) if last else stat.S_ISDIR(st.st_mode)):
            raise RootPolicyError(f"{component} is not a regular file or directory")
        if not _root_only(st):
            raise RootPolicyError(
                f"{component} can be changed by a user other than root "
                f"(uid {st.st_uid}, mode {stat.S_IMODE(st.st_mode):o}); "
                "refusing the policy"
            )
    try:
        raw = read_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise RootPolicyError(f"cannot read {path}: {exc}") from exc
    return parse_policy(raw)

"""Decide whether root may install a unit someone else wrote (#433).

A **positive** allowlist. Every section and directive a unit may carry is
listed here, and anything else refuses the unit. A denylist would miss the next
directive systemd adds; this cannot.

The rules exist because systemd (PID 1, root) acts on a unit before it drops to
``User=``: it reads ``EnvironmentFile=`` and credentials, opens
``StandardOutput=file:`` paths, creates and chowns ``LogsDirectory=``, and runs
``+``/``!`` commands with full privileges. A unit passes only if every such
action stays inside what the root policy (:mod:`fraisier.root_policy`) grants,
and every command runs as a non-root identity the policy names.

Every refusal carries the rule that refused it, so a test can tell "refused
for the right reason" from "refused".
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fraisier.root_policy import RootPolicy

RULE_SYNTAX = "syntax"
RULE_SECTION = "section"
RULE_DIRECTIVE = "directive"
RULE_USER = "user"
RULE_GROUP = "group"
RULE_EXEC_PREFIX = "exec-prefix"
RULE_EXEC_PATH = "exec-path"
RULE_READ_PATH = "read-path"
RULE_ENVIRONMENT = "environment"
RULE_STDIO = "stdio"
RULE_DIRECTORY = "directory"
RULE_UNIT_NAME = "unit-name"
RULE_UNIT_SUFFIX = "unit-suffix"


@dataclass(frozen=True)
class Refusal:
    rule: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        where = f"line {self.line}: " if self.line is not None else ""
        return f"[{self.rule}] {where}{self.message}"


_ANY = None
"""A directive whose value systemd never acts on as root beyond the unit."""

_UNIT_REFS = "unit-refs"
_EXEC = "exec"

_UNIT_SECTION: dict[str, object] = {
    "Description": _ANY,
    "Documentation": _ANY,
    "After": _UNIT_REFS,
    "Before": _UNIT_REFS,
    "Requires": _UNIT_REFS,
    "Requisite": _UNIT_REFS,
    "Wants": _UNIT_REFS,
    "BindsTo": _UNIT_REFS,
    "PartOf": _UNIT_REFS,
    "OnFailure": _UNIT_REFS,
    "OnSuccess": _UNIT_REFS,
    "StartLimitIntervalSec": _ANY,
    "StartLimitBurst": _ANY,
}

EXEC_KEYS = (
    "ExecCondition",
    "ExecStartPre",
    "ExecStart",
    "ExecStartPost",
    "ExecReload",
    "ExecStop",
    "ExecStopPost",
)

DIRECTORY_KEYS = (
    "RuntimeDirectory",
    "StateDirectory",
    "CacheDirectory",
    "LogsDirectory",
    "ConfigurationDirectory",
)

_SERVICE_SECTION: dict[str, object] = {
    **dict.fromkeys(EXEC_KEYS, _EXEC),
    **dict.fromkeys(DIRECTORY_KEYS, RULE_DIRECTORY),
    "User": RULE_USER,
    "Group": RULE_GROUP,
    "EnvironmentFile": RULE_READ_PATH,
    "LoadCredential": RULE_READ_PATH,
    "Environment": RULE_ENVIRONMENT,
    "StandardInput": RULE_STDIO,
    "StandardOutput": RULE_STDIO,
    "StandardError": RULE_STDIO,
    **dict.fromkeys(
        (
            "Type",
            "WorkingDirectory",
            "Restart",
            "RestartSec",
            "RemainAfterExit",
            "KillMode",
            "KillSignal",
            "TimeoutSec",
            "TimeoutStartSec",
            "TimeoutStopSec",
            "WatchdogSec",
            "MemoryMax",
            "MemoryHigh",
            "CPUQuota",
            "CPUAccounting",
            "TasksMax",
            "LimitNOFILE",
            "Nice",
            "UMask",
            "SyslogIdentifier",
            "RuntimeDirectoryMode",
            "RuntimeDirectoryPreserve",
            "StateDirectoryMode",
            "CacheDirectoryMode",
            "LogsDirectoryMode",
            "ConfigurationDirectoryMode",
            # Mount-namespace views: they narrow what the unit sees, and grant
            # no permission the unit's user does not already have.
            "ReadWritePaths",
            "ReadOnlyPaths",
            "InaccessiblePaths",
            # Sandboxing. Weakening one only weakens a non-root unit.
            "NoNewPrivileges",
            "ProtectSystem",
            "ProtectHome",
            "PrivateTmp",
            "PrivateDevices",
            "PrivateNetwork",
            "ProtectKernelTunables",
            "ProtectKernelModules",
            "ProtectKernelLogs",
            "ProtectControlGroups",
            "ProtectHostname",
            "ProtectClock",
            "RestrictRealtime",
            "RestrictNamespaces",
            "RestrictSUIDSGID",
            "RestrictAddressFamilies",
            "LockPersonality",
            "MemoryDenyWriteExecute",
            "RemoveIPC",
            "SystemCallFilter",
            "SystemCallArchitectures",
            "SystemCallErrorNumber",
        ),
        _ANY,
    ),
}

_TIMER_SECTION: dict[str, object] = {
    **dict.fromkeys(
        (
            "OnCalendar",
            "OnBootSec",
            "OnStartupSec",
            "OnActiveSec",
            "OnUnitActiveSec",
            "OnUnitInactiveSec",
            "AccuracySec",
            "RandomizedDelaySec",
            "FixedRandomDelay",
            "Persistent",
        ),
        _ANY,
    ),
    "Unit": _UNIT_REFS,
}

_INSTALL_SECTION: dict[str, object] = {
    "WantedBy": _UNIT_REFS,
    "RequiredBy": _UNIT_REFS,
}

_SECTIONS: dict[str, dict[str, dict[str, object]]] = {
    ".service": {
        "Unit": _UNIT_SECTION,
        "Service": _SERVICE_SECTION,
        "Install": _INSTALL_SECTION,
    },
    ".timer": {
        "Unit": _UNIT_SECTION,
        "Timer": _TIMER_SECTION,
        "Install": _INSTALL_SECTION,
    },
}

UNIT_SUFFIXES = tuple(_SECTIONS)

#: Exec prefixes systemd documents. ``-`` ignores failure, ``@`` sets argv[0]
#: and ``:`` skips variable expansion; none changes who runs the command. ``+``
#: and ``!`` run it privileged, ``!!`` is refused with them (it is ignored on
#: current systemd, but has meant "privileged" before), and ``|`` runs it
#: through a shell systemd chooses.
_EXEC_PREFIX_CHARS = frozenset("-@:+!|")
_HARMLESS_EXEC_PREFIXES = frozenset("-@:")

#: Variables that make a process load code from a path: the dynamic loader's
#: and CPython's. A non-root unit runs as its own user either way; this keeps
#: a unit from loading code from somewhere its command does not name.
_LOADER_VARIABLES = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONPLATLIBDIR",
        "PYTHONEXECUTABLE",
        "PYTHONBREAKPOINT",
    }
)

#: Destinations systemd opens without a path. ``file:``, ``append:`` and
#: ``truncate:`` make PID 1 open a path as root; ``tty`` opens a terminal.
_SAFE_STDIO = frozenset(
    {"inherit", "null", "journal", "kmsg", "journal+console", "kmsg+console", "socket"}
)

_UNIT_REF_RE = re.compile(r"^[A-Za-z0-9:_.\\@%-]+$")
_DIRECTIVE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _is_root_identity(value: str) -> bool:
    return value == "root" or (value.isdigit() and int(value) == 0)


def _logical_lines(text: str) -> list[tuple[int, str]] | Refusal:
    """Join continuation lines the way systemd does; drop comments."""
    out: list[tuple[int, str]] = []
    pending: list[str] = []
    start = 0
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip("\r")
        stripped = line.strip()
        if pending and stripped.startswith(("#", ";")):
            continue
        if not pending and (not stripped or stripped.startswith(("#", ";"))):
            continue
        if not pending:
            start = number
        if line.endswith("\\"):
            pending.append(line[:-1])
            continue
        pending.append(line)
        out.append((start, " ".join(p.strip() for p in pending)))
        pending = []
    if pending:
        return Refusal(RULE_SYNTAX, "the file ends inside a continued line", start)
    return out


def unit_directives(text: str) -> list[tuple[str, str, str]]:
    """``(section, key, value)`` for every assignment, as systemd reads them.

    Best effort: what cannot be parsed is skipped, because the caller is
    *observing* a unit, and :func:`validate_unit` is what judges it.
    """
    lines = _logical_lines(text)
    if isinstance(lines, Refusal):
        return []
    section = ""
    out: list[tuple[str, str, str]] = []
    for _number, line in lines:
        if line.startswith("["):
            section = line.strip().strip("[]")
            continue
        key, sep, value = line.partition("=")
        if sep:
            out.append((section, key.strip(), value.strip()))
    return out


def exec_executable(value: str) -> str | None:
    """The executable an ``Exec*=`` value runs, prefixes stripped; None if none."""
    command = value.lstrip("".join(_EXEC_PREFIX_CHARS))
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    return argv[0] if argv else None


def _exec_refusal(value: str, policy: RootPolicy, key: str) -> Refusal | None:
    if not value:
        return None  # An empty assignment resets the list.
    index = 0
    while index < len(value) and value[index] in _EXEC_PREFIX_CHARS:
        index += 1
    prefix, command = value[:index], value[index:]
    if not set(prefix) <= _HARMLESS_EXEC_PREFIXES:
        return Refusal(
            RULE_EXEC_PREFIX,
            f"{key}= prefix {prefix!r} runs the command with root privileges",
        )
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return Refusal(RULE_SYNTAX, f"{key}= cannot be parsed: {exc}")
    if not argv:
        return Refusal(RULE_EXEC_PATH, f"{key}= names no command")
    executable = argv[0]
    if (
        not posixpath.isabs(executable)
        or "%" in executable
        or "$" in executable
        or posixpath.normpath(executable) != executable
    ):
        return Refusal(
            RULE_EXEC_PATH,
            f"{key}= executable {executable!r} is not a normalised absolute path",
        )
    for granted in policy.exec_prefixes:
        if granted.endswith("/") and executable.startswith(granted):
            return None
        if executable == granted:
            return None
    return Refusal(
        RULE_EXEC_PATH,
        f"{key}= executable {executable} is outside the root policy's exec_prefixes",
    )


def _read_path_refusal(key: str, value: str, policy: RootPolicy) -> Refusal | None:
    if key == "EnvironmentFile":
        path = value.removeprefix("-")
    else:
        # Without a path, systemd reads ID from a credential store it
        # chooses; "" is never in the policy, so that is refused here too.
        path = value.partition(":")[2]
    if path in policy.read_paths:
        return None
    return Refusal(
        RULE_READ_PATH,
        f"{key}= makes root read {path!r}, which the root policy does not list",
    )


def _environment_refusal(value: str) -> Refusal | None:
    try:
        assignments = shlex.split(value)
    except ValueError as exc:
        return Refusal(RULE_SYNTAX, f"Environment= cannot be parsed: {exc}")
    for assignment in assignments:
        name = assignment.partition("=")[0]
        if name.startswith("LD_") or name in _LOADER_VARIABLES:
            return Refusal(
                RULE_ENVIRONMENT, f"Environment= sets loader variable {name}"
            )
    return None


def _value_refusal(
    kind: object, key: str, value: str, policy: RootPolicy
) -> Refusal | None:
    if kind is _ANY:
        return None
    if kind is _EXEC:
        return _exec_refusal(value, policy, key)
    if kind is _UNIT_REFS:
        bad = [t for t in value.split() if not _UNIT_REF_RE.match(t)]
        if bad:
            return Refusal(RULE_SYNTAX, f"{key}= names {bad[0]!r}, not a unit name")
        return None
    if kind == RULE_USER:
        if not value or _is_root_identity(value) or value not in policy.users:
            return Refusal(
                RULE_USER, f"User={value!r} is not a non-root user the policy lists"
            )
        return None
    if kind == RULE_GROUP:
        if not value or _is_root_identity(value) or value not in policy.groups:
            return Refusal(
                RULE_GROUP,
                f"Group={value!r} is not a non-root group the policy lists",
            )
        return None
    if kind == RULE_READ_PATH:
        return _read_path_refusal(key, value, policy)
    if kind == RULE_ENVIRONMENT:
        return _environment_refusal(value)
    if kind == RULE_STDIO:
        if value not in _SAFE_STDIO:
            return Refusal(RULE_STDIO, f"{key}={value} makes root open it")
        return None
    if kind == RULE_DIRECTORY:
        bad = [t for t in value.split() if t not in policy.directories]
        if bad:
            return Refusal(
                RULE_DIRECTORY,
                f"{key}={bad[0]} would be created and chowned by root, and the "
                "root policy does not list it",
            )
        return None
    raise AssertionError(f"unhandled directive kind {kind!r}")  # pragma: no cover


def _safe_name(key: str) -> str:
    """Echo a directive name only if it looks like one (never file content)."""
    return key if _DIRECTIVE_RE.match(key) and len(key) <= 64 else "<unreadable>"


def validate_unit(name: str, text: str, policy: RootPolicy) -> list[Refusal]:
    """Every reason root must not install *text* as unit *name*. Empty = allowed."""
    suffix = next((s for s in UNIT_SUFFIXES if name.endswith(s)), None)
    if suffix is None:
        return [Refusal(RULE_UNIT_SUFFIX, f"{name} is not a .service or .timer")]
    if _CONTROL_RE.search(text):
        return [Refusal(RULE_SYNTAX, "the file contains control characters")]
    lines = _logical_lines(text)
    if isinstance(lines, Refusal):
        return [lines]

    sections = _SECTIONS[suffix]
    refusals: list[Refusal] = []
    current: dict[str, object] | None = None
    users: list[str] = []
    for number, line in lines:
        if line.startswith("["):
            section = line.strip()
            if not section.endswith("]") or section[1:-1] not in sections:
                refusals.append(
                    Refusal(
                        RULE_SECTION, f"section {section[:40]!r} is not allowed", number
                    )
                )
                current = {}
            else:
                current = sections[section[1:-1]]
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not _DIRECTIVE_RE.match(key):
            refusals.append(Refusal(RULE_SYNTAX, "not a directive assignment", number))
            continue
        if current is None:
            refusals.append(
                Refusal(RULE_SYNTAX, f"{key}= appears before any section", number)
            )
            continue
        if not current:
            continue  # Already refused for its section.
        if key not in current:
            refusals.append(
                Refusal(
                    RULE_DIRECTIVE,
                    f"{_safe_name(key)}= is not on the allowlist for this section",
                    number,
                )
            )
            continue
        if key == "User":
            users.append(value)
        refusal = _value_refusal(current[key], key, value, policy)
        if refusal is not None:
            refusals.append(
                Refusal(refusal.rule, refusal.message, refusal.line or number)
            )

    if suffix == ".service" and not users:
        refusals.append(
            Refusal(RULE_USER, "no User=: the unit would run every command as root")
        )
    return refusals


def check_scaffold_unit_name(name: str, policy: RootPolicy) -> Refusal | None:
    """A unit a deploy may install from the scaffold tree: one the policy grants."""
    if name in policy.units and name.endswith(UNIT_SUFFIXES) and "/" not in name:
        return None
    return Refusal(
        RULE_UNIT_NAME,
        f"{name} is not a unit the root policy lets a deploy install; "
        "an operator installs it with `sudo fraisier scaffold-install`",
    )


def check_app_unit_name(name: str, policy: RootPolicy) -> Refusal | None:
    """A unit the unit-installer may copy from the app's tree (#433, D7)."""
    if "/" in name or not name.endswith(UNIT_SUFFIXES):
        return Refusal(RULE_UNIT_SUFFIX, f"{name} is not a .service or .timer unit")
    taken = set(policy.units) | {
        posixpath.basename(dest) for dest in policy.operator_only
    }
    if name in taken:
        return Refusal(
            RULE_UNIT_NAME,
            f"{name} is a unit fraisier's scaffold installs; an app may not replace it",
        )
    return None

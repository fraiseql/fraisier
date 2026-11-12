"""Part of the scaffold: one category, or one file (#501).

``scaffold-install``, ``scaffold-diff`` and the rendered ``install.sh`` take
``--only <category>`` (repeatable) or ``--path <file>``. Without either they
cover everything, as before.

A category is read off where a file is installed, so the artifact manifest, the
root policy and a deploy's pending list all classify a file the same way:
``/etc/nginx`` is nginx, ``/etc/sudoers.d`` is sudoers, ``/etc/systemd/system``
is systemd. ``scripts`` and ``ci`` are accepted names that select nothing root
installs: those files are used in place from the scaffold tree a deploy renders.

The filter itself runs in ``install.sh``, so ``nginx -t`` before a reload and
``visudo -c`` before a sudoers install stay in one script. Here: validating the
filter, narrowing what is shown, and the root policy a partial install writes,
which changes the selected entries only, so it keeps matching what is installed.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from fraisier.scaffold.artifacts import NGINX_AVAILABLE, SYSTEMD_DIR

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from fraisier.root_policy import RootPolicy

CATEGORIES = ("nginx", "systemd", "sudoers", "scripts", "ci")

#: Destination root → category. Every root-owned file scaffold-install writes
#: is under one of these.
_ROOTS = (
    (posixpath.dirname(NGINX_AVAILABLE) + "/", "nginx"),
    ("/etc/sudoers.d/", "sudoers"),
    (SYSTEMD_DIR + "/", "systemd"),
)
_INSTALLED = tuple(category for _root, category in _ROOTS)
_UNIT_SUFFIXES = (".service", ".socket", ".timer", ".path", ".mount", ".target")


class SelectionError(ValueError):
    """The filter names nothing this install can act on. Nothing ran."""


def category_of(destination: str) -> str | None:
    """The category of a root-owned file, from where it is installed."""
    for root, category in _ROOTS:
        if destination.startswith(root):
            return category
    return None


@dataclass(frozen=True)
class Selection:
    """``--only`` categories, or one ``--path`` relative to the rendered tree."""

    categories: frozenset[str] = frozenset()
    path: str | None = None

    def covers(self, destination: str, source: str | None = None) -> bool:
        if self.path is not None:
            return source == self.path
        return category_of(destination) in self.categories

    def install_args(self) -> list[str]:
        """The same filter, as ``install.sh`` and the CLI spell it."""
        if self.path is not None:
            return ["--path", self.path]
        return [
            arg
            for category in CATEGORIES
            if category in self.categories
            for arg in ("--only", category)
        ]


def parse_selection(only: Sequence[str], path: str | None) -> Selection | None:
    """Validate ``--only`` / ``--path``; ``None`` means the whole scaffold."""
    if only and path is not None:
        raise SelectionError("give either --only or --path, not both")
    if path is not None:
        normal = posixpath.normpath(path) if path else ""
        if (
            not path
            or path.startswith("/")
            or normal != path
            or normal.split("/")[0] in {"..", "."}
        ):
            raise SelectionError(
                f"--path {path!r} must name a file relative to the rendered "
                "scaffold tree, such as nginx/<vhost>.conf or systemd/<unit>"
            )
        return Selection(path=path)
    if not only:
        return None
    unknown = sorted(set(only) - set(CATEGORIES))
    if unknown:
        raise SelectionError(
            f"unknown category {', '.join(unknown)}: choose from "
            f"{', '.join(CATEGORIES)}"
        )
    return Selection(categories=frozenset(only))


def select_artifacts(chosen: Iterable[Mapping], selection: Selection) -> list:
    """This host's installed artifacts the filter selects; refuses an empty pick."""
    chosen = list(chosen)
    picked = [a for a in chosen if selection.covers(a["destination"], a["source"])]
    if picked:
        return picked
    if selection.path is not None:
        installable = ", ".join(sorted(a["source"] for a in chosen)) or "nothing"
        raise SelectionError(
            f"--path {selection.path} is not a root-owned file this host "
            f"installs. This host installs: {installable}"
        )
    names = ", ".join(sorted(selection.categories))
    if not selection.categories & set(_INSTALLED):
        raise SelectionError(
            f"--only {names} selects no root-owned file: scripts and the CI "
            "workflow are used in place from the scaffold tree a deploy renders. "
            "scaffold-install installs nginx, systemd and sudoers files."
        )
    raise SelectionError(f"--only {names} selects no root-owned file on this host")


def merge_policy(
    current: RootPolicy,
    draft: RootPolicy,
    selection: Selection,
    *,
    selected: Iterable[Mapping] = (),
) -> RootPolicy:
    """The policy after installing *selection* of *draft* over *current*.

    Entries the selection covers come from the draft (including their removal);
    every other entry keeps what the operator last approved. The identities,
    executables, files and directories units may use follow the units: taken
    from the draft when all of systemd is selected, widened by it when one unit
    is, kept when no unit is.
    """
    if selection.path is not None:
        paths = {a["destination"] for a in selected}

        def picked(destination: str) -> bool:
            return destination in paths
    else:

        def picked(destination: str) -> bool:
            return category_of(destination) in selection.categories

    def unit_dest(name: str) -> str:
        return f"{SYSTEMD_DIR}/{name}"

    units = {n: g for n, g in current.units.items() if not picked(unit_dest(n))}
    units |= {n: g for n, g in draft.units.items() if picked(unit_dest(n))}
    operator_only = {d: s for d, s in current.operator_only.items() if not picked(d)}
    operator_only |= {d: s for d, s in draft.operator_only.items() if picked(d)}

    merged = replace(
        current,
        project=draft.project,
        scaffold_dir=draft.scaffold_dir,
        units=dict(sorted(units.items())),
        operator_only=dict(sorted(operator_only.items())),
    )
    if selection.path is None and "systemd" in selection.categories:
        return replace(
            merged,
            users=draft.users,
            groups=draft.groups,
            exec_prefixes=draft.exec_prefixes,
            read_paths=draft.read_paths,
            directories=draft.directories,
        )
    if selection.path is not None and any(category_of(d) == "systemd" for d in paths):
        return replace(
            merged,
            users=current.users | draft.users,
            groups=current.groups | draft.groups,
            exec_prefixes=tuple(
                sorted(set(current.exec_prefixes) | set(draft.exec_prefixes))
            ),
            read_paths=current.read_paths | draft.read_paths,
            directories=current.directories | draft.directories,
        )
    return merged


def _pending_category(item: str) -> str | None:
    """The category of one ``pending`` line of the scaffold-install helper."""
    subject = item.split(": ", 1)[0]
    if subject.startswith("/"):
        return category_of(subject)
    if "/" not in subject and subject.endswith(_UNIT_SUFFIXES):
        return "systemd"
    return None


def operator_remedy(pending: Sequence[str]) -> str:
    """What an operator runs to clear *pending*, naming its categories."""
    categories = {_pending_category(item) for item in pending}
    if not pending or None in categories:
        return (
            "These files are installed by an operator: run `sudo fraisier "
            "scaffold-install` on this host (it shows the diff and asks), "
            "then deploy again."
        )
    names = [c for c in CATEGORIES if c in categories]
    only = " ".join(f"--only {c}" for c in names)
    return (
        f"These files are installed by an operator (pending: {', '.join(names)}): "
        f"run `sudo fraisier scaffold-install {only}` on this host (it shows "
        "the diff and asks), then deploy again."
    )

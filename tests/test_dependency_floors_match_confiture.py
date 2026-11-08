"""No floor fraisier declares is below one confiture requires.

A floor is a promise that the version installs and runs. When fraisier and the
confiture it pins both name a package, the resolver takes the higher floor, so a
lower one in fraisier's metadata promises versions that can never be installed
beside that confiture. confiture 1.46.0 raised ``psycopg`` to 3.3.0 (it executes
template strings) and ``typer`` to 0.24, which requires ``click>=8.2.1``; a
fraisier still declaring ``psycopg>=3.1.0`` and ``click>=8.1.0`` was claiming
support it never had.

confiture's own requirements and those of its direct dependencies are read from
the installed metadata, so ``click`` (required by ``typer``) is covered too.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import requires
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _floor(requirement: Requirement) -> Version | None:
    floors = [
        Version(clause.version)
        for clause in requirement.specifier
        if clause.operator in (">=", "==", "~=")
    ]
    return max(floors, default=None)


def _runtime(distribution: str) -> list[Requirement]:
    """*distribution*'s requirements outside any extra."""
    found = [Requirement(raw) for raw in requires(distribution) or []]
    return [r for r in found if r.marker is None or "extra" not in str(r.marker)]


def _confiture_floors() -> dict[str, Version]:
    """name → the highest floor confiture or one of its direct dependencies declares."""
    floors: dict[str, Version] = {}
    direct = _runtime("fraiseql-confiture")
    for requirement in [
        *direct,
        *(dep for r in direct for dep in _runtime(r.name)),
    ]:
        floor = _floor(requirement)
        name = canonicalize_name(requirement.name)
        if floor is not None and floor > floors.get(name, Version("0")):
            floors[name] = floor
    return floors


def _declared() -> list[tuple[str, Requirement]]:
    """Every requirement fraisier declares, labelled with where it is declared."""
    project = tomllib.loads(_PYPROJECT.read_text())["project"]
    declared = [("dependencies", Requirement(raw)) for raw in project["dependencies"]]
    for extra, raws in project["optional-dependencies"].items():
        declared += [(f"[{extra}]", Requirement(raw)) for raw in raws]
    return declared


_SHARED = [
    (where, requirement)
    for where, requirement in _declared()
    if canonicalize_name(requirement.name) in _confiture_floors()
]


def test_fraisier_shares_dependencies_with_confiture() -> None:
    """The check below has something to check: psycopg and click at least."""
    names = {canonicalize_name(r.name) for _, r in _SHARED}
    assert {"psycopg", "click"} <= names


@pytest.mark.parametrize(
    ("where", "requirement"), _SHARED, ids=[f"{w}:{r.name}" for w, r in _SHARED]
)
def test_a_shared_floor_is_not_below_confiture_s(
    where: str, requirement: Requirement
) -> None:
    required = _confiture_floors()[canonicalize_name(requirement.name)]
    declared = _floor(requirement)
    assert declared is not None, f"{where} {requirement} declares no floor"
    assert declared >= required, (
        f"{where} declares {requirement}, but the confiture fraisier pins "
        f"requires {requirement.name}>={required}: no version below it installs"
    )

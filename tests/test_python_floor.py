"""Python 3.14 is the floor, and everything that states it agrees (#435).

confiture 1.30.0 requires 3.14, so fraisier cannot honestly claim less: a wider
``requires-python`` would admit an interpreter on which the resolver can only
fail, and a CI leg on 3.13 would test a combination nobody can install.  Each
place that names a Python is read here, so lowering one of them alone fails.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.specifiers import SpecifierSet

from fraisier.doctor import PYTHON_FLOOR

ROOT = Path(__file__).resolve().parent.parent
FLOOR = ".".join(str(p) for p in PYTHON_FLOOR)
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def test_requires_python_is_the_floor() -> None:
    declared = SpecifierSet(PYPROJECT["project"]["requires-python"])
    assert declared == SpecifierSet(f">={FLOOR}")


def test_the_only_python_classifier_is_the_floor() -> None:
    classifiers = [
        c
        for c in PYPROJECT["project"]["classifiers"]
        if c.startswith("Programming Language :: Python :: 3.")
    ]
    assert classifiers == [f"Programming Language :: Python :: {FLOOR}"]


def test_ruff_targets_the_floor() -> None:
    assert PYPROJECT["tool"]["ruff"]["target-version"] == f"py{FLOOR.replace('.', '')}"


def _python_versions(node: object) -> list[str]:
    """Every ``python-version`` value under *node*, matrix lists flattened."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "python-version":
                found.extend(
                    str(v) for v in (value if isinstance(value, list) else [value])
                )
            else:
                found.extend(_python_versions(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_python_versions(item))
    return found


def test_the_workflows_are_found() -> None:
    assert {p.name for p in WORKFLOWS} >= {
        "publish.yml",
        "python-version-matrix.yml",
        "quality-gate.yml",
    }


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_runs_only_on_the_floor(path: Path) -> None:
    versions = _python_versions(yaml.safe_load(path.read_text()))
    # `${{ matrix.python-version }}` is a reference, not a version.
    literal = [v for v in versions if not v.startswith("${{")]
    assert literal, f"{path.name} names no Python at all"
    assert set(literal) == {FLOOR}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_step_names_an_older_python(path: Path) -> None:
    stale = re.findall(r"\b3\.(?:1[0-3]|[0-9])\b(?!\d)", path.read_text())
    # `3.9` etc. would be a Python; version numbers of actions are `@v6`.
    assert not stale, f"{path.name} still mentions Python {stale}"


@pytest.mark.parametrize(
    "doc", ["README.md", "development.md", "docs/deployment-guide.md"]
)
def test_the_docs_state_the_floor(doc: str) -> None:
    text = (ROOT / doc).read_text()
    assert re.search(rf"Python(\*\*)?:? {re.escape(FLOOR)}\+", text)

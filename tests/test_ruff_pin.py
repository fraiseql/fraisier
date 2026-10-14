"""The pre-commit ruff hook runs the ruff that ``uv.lock`` pins (#404).

The ``ruff-pre-commit`` rev is the pin CI lints with: ``quality-gate.yml`` and
``publish.yml`` install ``ruff==`` that rev (#361).  ``uv run ruff``, the local
gate, installs from ``uv.lock`` instead, which is resolved separately.  An
autoupdate that moved only the rev would let a commit pass the local ruff and
fail CI's, as 0.16.1 against 0.16.6 once did, so the two are read here and must
name the same version.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RUFF_HOOK_REPO = "https://github.com/astral-sh/ruff-pre-commit"


def _locked_ruff() -> str:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = [p["version"] for p in lock["package"] if p["name"] == "ruff"]
    assert len(versions) == 1, f"uv.lock pins ruff {versions}, expected one"
    return versions[0]


def _hook_ruff() -> str:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    revs = [r["rev"] for r in config["repos"] if r["repo"] == RUFF_HOOK_REPO]
    assert len(revs) == 1, f"{RUFF_HOOK_REPO} appears {len(revs)} times"
    return revs[0].removeprefix("v")


def test_the_pre_commit_hook_runs_the_locked_ruff() -> None:
    assert _hook_ruff() == _locked_ruff()

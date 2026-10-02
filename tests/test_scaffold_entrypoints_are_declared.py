"""Every binary a scaffold template runs is a console script the wheel installs.

The unit-installer units ran ``~/.local/bin/fraisier-unit-installer`` while
``[project.scripts]`` never declared it, so ``uv tool install`` never created it
and both units failed 203/EXEC the first time they were triggered. ``doctor``'s
``unit_entrypoints`` check only finds this on a host, after install.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "fraisier" / "scaffold" / "templates"


def _declared_scripts() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(project["project"]["scripts"])


def _referenced_binaries() -> set[str]:
    pattern = re.compile(r"\.local/bin/(fraisier[a-z-]*)")
    return {
        name
        for template in TEMPLATES.rglob("*")
        if template.is_file()
        for name in pattern.findall(
            template.read_text(encoding="utf-8", errors="ignore")
        )
    }


def test_templates_reference_at_least_the_main_cli() -> None:
    # Guard against the scan silently matching nothing.
    assert "fraisier" in _referenced_binaries()


def test_every_binary_a_template_runs_is_a_declared_console_script() -> None:
    missing = _referenced_binaries() - _declared_scripts()
    assert not missing, f"scaffold runs undeclared console scripts: {sorted(missing)}"

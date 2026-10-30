"""Every ``fraisier`` module imports in a fresh interpreter.

The test suite imports ``fraisier.config`` early, so an import cycle that
only bites when another module comes first stays hidden in it: a script or a
child that imports ``fraisier.dbops.restore`` on its own would fail.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _modules() -> list[str]:
    files = sorted((ROOT / "fraisier").rglob("*.py"))
    names = []
    for path in files:
        parts = path.relative_to(ROOT).with_suffix("").parts
        if parts[-1] == "__main__":
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


def test_every_module_imports_in_a_fresh_interpreter():
    modules = _modules()
    procs = {
        module: subprocess.Popen(
            [sys.executable, "-c", f"import {module}"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for module in modules
    }
    failures = {}
    for module, proc in procs.items():
        _, stderr = proc.communicate(timeout=120)
        if proc.returncode != 0:
            failures[module] = stderr.strip().splitlines()[-1]
    assert len(modules) > 100
    assert failures == {}

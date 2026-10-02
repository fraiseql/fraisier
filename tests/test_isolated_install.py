"""The built wheel works when installed alone (#427).

Hosts run fraisier from an isolated ``uv tool install``. The project venv this
suite runs in carries dozens of packages transitively, so an import that is not
declared in ``[project] dependencies`` passes every other test here and still
ships a CLI that dies on import: 0.83.0 and 0.84.0 imported ``packaging`` from
``fraisier.doctor`` without declaring it, and the webhook self-upgrade crashed
half-way on a production host.

This builds the wheel, installs it into an empty venv with nothing but its own
declared dependencies, and imports what the console scripts import.

It needs ``uv`` and an index to resolve from, so it runs where
``FRAISIER_INTEGRATION=1`` (the quality gate) and is skipped elsewhere. The
publish workflow repeats the same check on the artifact it is about to upload.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("FRAISIER_INTEGRATION") != "1" or shutil.which("uv") is None,
        reason="needs FRAISIER_INTEGRATION=1 and uv (network access to an index)",
    ),
]

REPO = Path(__file__).resolve().parent.parent

# Every module a console script in [project.scripts] starts from, plus the
# self-upgrade worker, which imports fraisier.doctor after an install.
STARTUP_MODULES = (
    "fraisier.cli",
    "fraisier.doctor",
    "fraisier.webhook",
    "fraisier.webhook_self_upgrade",
    "fraisier.systemctl_helper",
    "fraisier.install_helper",
    "fraisier.pgbackrest_helper",
    "fraisier.scaffold_install_helper",
)


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    return subprocess.run(
        list(args), cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module")
def isolated_venv(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("isolated-install")
    dist = root / "dist"
    built = _run("uv", "build", "--wheel", "--out-dir", str(dist), cwd=REPO)
    assert built.returncode == 0, built.stderr
    wheel = next(dist.glob("fraisier-*.whl"))
    venv = root / "venv"
    created = _run("uv", "venv", "--quiet", str(venv))
    assert created.returncode == 0, created.stderr
    installed = _run(
        "uv",
        "pip",
        "install",
        "--quiet",
        "--python",
        str(venv / "bin" / "python"),
        str(wheel),
    )
    assert installed.returncode == 0, installed.stderr
    return venv


def test_the_cli_starts(isolated_venv):
    proc = _run(str(isolated_venv / "bin" / "fraisier"), "--version")
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("module", STARTUP_MODULES)
def test_startup_modules_import_with_declared_dependencies_only(isolated_venv, module):
    proc = _run(str(isolated_venv / "bin" / "python"), "-c", f"import {module}")
    assert proc.returncode == 0, proc.stderr

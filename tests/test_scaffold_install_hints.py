"""Every hint that names scaffold-install names the command that works.

``scaffold-install`` refuses a non-root caller and renders its own tree
(#433), so ``fraisier scaffold-install`` without ``sudo`` exits 1, and a
``fraisier scaffold &&`` before it renders a tree nothing reads. Hints in the
deploy path, the CLI and the helpers still said both, so an operator who
followed one failed on the first try.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fraisier.errors import DeploymentError

_SOURCE = Path(__file__).parent.parent / "fraisier"
_COMMAND = "fraisier scaffold-install"


def _offending_lines() -> list[str]:
    """Each mention of the command not run under sudo, or behind a render."""
    found = []
    for path in sorted(_SOURCE.rglob("*")):
        if path.suffix not in {".py", ".j2", ".sh", ".md"} or not path.is_file():
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for match in re.finditer(re.escape(_COMMAND), line):
                before = line[: match.start()]
                if not before.endswith("sudo ") or "fraisier scaffold &&" in before:
                    found.append(
                        f"{path.relative_to(_SOURCE)}:{number}: {line.strip()}"
                    )
    return found


def test_every_scaffold_install_hint_runs_it_under_sudo():
    assert _offending_lines() == []


def test_a_pending_install_names_the_operator_command(tmp_path):
    from unittest.mock import MagicMock

    from fraisier.deployers.api import APIDeployer

    deployer = APIDeployer({"fraise_name": "api", "environment": "production"})
    reply = MagicMock(
        returncode=1,
        stdout="installed 0 unit(s), 3 unchanged",
        stderr="pending operator install: /etc/nginx/sites-available/x: differs",
        pending=["/etc/nginx/sites-available/x: differs from the render"],
        refused={},
        deferred_restarts=[],
    )
    deployer._try_scaffold_install_via_socket = lambda _path: reply  # ty: ignore[invalid-assignment]
    deployer._record_deferred_restarts = lambda *_a: None  # ty: ignore[invalid-assignment]

    with pytest.raises(
        DeploymentError, match=re.escape("sudo fraisier scaffold-install")
    ):
        deployer._install_scaffold(tmp_path / "fraises.yaml")

"""The suite ignores the git environment of whatever runs it.

``git commit`` runs the pre-commit hook with ``GIT_INDEX_FILE`` (and, from a
worktree, ``GIT_DIR``) exported, and the pytest hook passed them on. Every
test that builds its own repository then read and wrote the index of the
commit in progress: 22 errors under ``git commit`` that ``pre-commit run``
never showed, and run by hand with the variable set, the real index was
replaced by a test's one-file index.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent


def _git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def test_a_hooks_git_environment_reaches_no_test(tmp_path):
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q")
    (decoy / "kept.txt").write_text("kept\n")
    _git(decoy, "add", "kept.txt")
    _git(
        decoy,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-qm",
        "decoy",
    )
    index = decoy / ".git" / "index"
    before = index.read_bytes()

    env = os.environ.copy()
    env |= {
        "GIT_DIR": str(decoy / ".git"),
        "GIT_WORK_TREE": str(decoy),
        "GIT_INDEX_FILE": str(index),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "tests/test_worktree_sha_bare_model.py",
        ],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert index.read_bytes() == before, "a test wrote the caller's index"

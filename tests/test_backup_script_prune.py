"""The producing host's prune has #339's footgun too.

`backup.sh` ended with `find … -mtime +N -delete`: time-based, no floor.
A producer that stops producing ages its whole corpus out together, on
the machine that holds the only copies. Same bug class as
`cleanup_old_backups` before `keep_minimum`, same feature area, so it is
fixed in the same release rather than described in a follow-up.

Exercised by running the rendered script, not by reading it: the floor is
shell, and shell that looks right is not shell that works.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

import pytest
import yaml

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

_CONFIG: dict[str, Any] = {
    "name": "proj",
    "scaffold": {"deploy_user": "deployer"},
    "fraises": {
        "api": {
            "type": "api",
            "environments": {
                "production": {
                    "app_path": "/var/www/api",
                    "git_repo": "/var/git/api.git",
                }
            },
        },
        "worker": {
            "type": "api",
            "environments": {
                "production": {
                    "app_path": "/var/www/worker",
                    "git_repo": "/var/git/worker.git",
                }
            },
        },
    },
}


@pytest.fixture
def backup_sh(tmp_path):
    cfg = tmp_path / "fraises.yaml"
    config = {
        **_CONFIG,
        "scaffold": {**_CONFIG["scaffold"], "output_dir": str(tmp_path / "out")},
    }
    cfg.write_text(yaml.safe_dump(config))
    ScaffoldRenderer(FraisierConfig(cfg)).render()
    script = tmp_path / "out" / "backup.sh"
    script.chmod(0o755)
    return script


def aged_dump(directory, name: str, *, days: float):
    path = directory / name
    path.write_text("dump")
    when = time.time() - days * 86400
    os.utime(path, (when, when))
    return path


def run_backup(script, backup_dir, tmp_path):
    """Run the rendered script with pg_dump stubbed and BACKUP_DIR redirected."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "pg_dump"
    fake.write_text("#!/bin/bash\necho 'fake dump payload'\n")
    fake.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FRAISIER_BACKUP_DIR"] = str(backup_dir)

    return subprocess.run(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


class TestBackupScriptPrune:
    def test_backup_script_prune_keeps_a_minimum(self, backup_sh, tmp_path):
        """Five dumps, every one past the window: three survive, not zero.

        The stalled-producer case, which is the only one the floor exists
        for. With no floor this run empties the corpus on the machine
        holding the only copies.

        The script takes today's backup *before* pruning, so the fresh dump
        legitimately occupies the first floor slot and two of the five old
        ones fill the rest. The invariant is the count, not which files.
        """
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        old = [
            aged_dump(corpus, f"api_2025010{n}_000000.sql.gz", days=days)
            for n, days in enumerate((40, 50, 60, 70, 80), start=1)
        ]

        result = run_backup(backup_sh, corpus, tmp_path)
        assert result.returncode == 0, result.stderr

        assert len(list(corpus.glob("api_*.sql.gz"))) == 3, "floor did not hold"
        # Newest-first, so the two oldest of the five are the ones that go.
        assert not old[4].exists()
        assert not old[3].exists()
        assert old[0].exists(), "an exempted dump was deleted"

    def test_dumps_inside_the_window_are_untouched(self, backup_sh, tmp_path):
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        fresh = aged_dump(corpus, "api_20250601_000000.sql.gz", days=1)

        result = run_backup(backup_sh, corpus, tmp_path)
        assert result.returncode == 0, result.stderr
        assert fresh.exists()

    def test_the_floor_is_per_fraise(self, backup_sh, tmp_path):
        """A shared floor would leave some fraises with nothing.

        All ten dumps share one directory. Counting three across the whole
        corpus would keep three of `worker` and none of `api` — the exact
        outcome the floor exists to prevent, arrived at differently.
        """
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        for day in (40, 50, 60, 70):
            aged_dump(corpus, f"api_202501{day}_000000.sql.gz", days=day)
            aged_dump(corpus, f"worker_202501{day}_000000.sql.gz", days=day + 100)

        result = run_backup(backup_sh, corpus, tmp_path)
        assert result.returncode == 0, result.stderr

        assert len(list(corpus.glob("api_*.sql.gz"))) == 3
        assert len(list(corpus.glob("worker_*.sql.gz"))) == 3

    def test_old_dumps_past_the_floor_are_still_removed(self, backup_sh, tmp_path):
        """The floor exempts; it does not disable the age rule."""
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        doomed = aged_dump(corpus, "api_20250101_000000.sql.gz", days=200)
        for day in (40, 50, 60):
            aged_dump(corpus, f"api_202502{day}_000000.sql.gz", days=day)

        result = run_backup(backup_sh, corpus, tmp_path)
        assert result.returncode == 0, result.stderr
        assert not doomed.exists(), "the oldest dump past the floor survived"

    def test_an_empty_corpus_is_not_an_error(self, backup_sh, tmp_path):
        """`set -euo pipefail` plus an unmatched glob is a script that aborts
        before it has taken today's backup."""
        corpus = tmp_path / "corpus"
        corpus.mkdir()

        result = run_backup(backup_sh, corpus, tmp_path)
        assert result.returncode == 0, result.stderr

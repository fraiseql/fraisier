"""fraisier runs the ``confiture`` from its own venv, never PATH's (#456).

Bootstrap installs fraisier with ``uv tool install``, which exposes fraisier's
entry points only: the ``confiture`` fraisier pins sits next to the tool's
interpreter and nowhere on PATH.  Preflight already resolved it there (#190);
the drift gate, the ``confiture_*`` wrappers and doctor ran a bare
``confiture`` and got either nothing or a version fraisier never audited.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from fraisier import doctor
from fraisier.dbops import confiture as confiture_cli
from fraisier.dbops.confiture_executable import confiture_executable
from fraisier.dbops.preflight import _run_confiture_preflight

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def venv_bin(tmp_path, monkeypatch) -> Path:
    """A venv whose ``bin/`` holds an interpreter and an executable confiture."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").touch()
    exe = bin_dir / "confiture"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))
    return bin_dir


def _proc(stdout: str = "{}", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class TestResolution:
    def test_the_confiture_beside_the_interpreter(self, venv_bin) -> None:
        assert confiture_executable() == str(venv_bin / "confiture")

    def test_path_only_when_the_venv_has_none(self, venv_bin, caplog) -> None:
        (venv_bin / "confiture").unlink()
        with caplog.at_level(logging.WARNING):
            assert confiture_executable() == "confiture"
        assert str(venv_bin) in caplog.text

    def test_a_file_that_cannot_run_is_not_taken(self, venv_bin) -> None:
        (venv_bin / "confiture").chmod(0o644)
        assert confiture_executable() == "confiture"

    def test_a_directory_is_not_taken(self, venv_bin) -> None:
        (venv_bin / "confiture").unlink()
        (venv_bin / "confiture").mkdir()
        assert confiture_executable() == "confiture"

    def test_the_venv_not_the_base_interpreter(
        self, venv_bin, tmp_path, monkeypatch
    ) -> None:
        """A venv's ``python`` links to a base install that has no confiture."""
        base = tmp_path / "base" / "bin"
        base.mkdir(parents=True)
        (base / "python3.14").touch()
        (venv_bin / "python").unlink()
        (venv_bin / "python").symlink_to(base / "python3.14")
        monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
        assert confiture_executable() == str(venv_bin / "confiture")


class TestEveryCallerRunsIt:
    def test_migrate(self, venv_bin) -> None:
        with patch("subprocess.run", return_value=_proc()) as run:
            confiture_cli.confiture_migrate()
        assert run.call_args[0][0][0] == str(venv_bin / "confiture")

    def test_rebuild(self, venv_bin) -> None:
        with patch("subprocess.run", return_value=_proc()) as run:
            confiture_cli.confiture_rebuild()
        assert run.call_args[0][0][0] == str(venv_bin / "confiture")

    def test_status(self, venv_bin) -> None:
        with patch("subprocess.run", return_value=_proc()) as run:
            confiture_cli.confiture_status()
        assert run.call_args[0][0][0] == str(venv_bin / "confiture")

    def test_preflight_falls_back_like_the_rest(self, venv_bin) -> None:
        (venv_bin / "confiture").unlink()
        payload = '{"ok": true, "summary": {}, "issues": []}'
        with patch("subprocess.run", return_value=_proc(payload)) as run:
            _run_confiture_preflight(Path("c.yaml"), Path("m"), "postgresql:///x")
        assert run.call_args[0][0][0] == "confiture"

    def test_doctor_checks_the_binary_fraisier_runs(self, venv_bin) -> None:
        with patch("subprocess.run", return_value=_proc("confiture 1.33.0")) as run:
            result = doctor.DOCTOR_CHECKS["confiture_version"].fn(None)
        assert run.call_args[0][0][0] == str(venv_bin / "confiture")
        assert result.status == "pass"

    def test_doctor_reads_the_gates_version_from_it(self, venv_bin) -> None:
        with patch("subprocess.run", return_value=_proc("confiture 1.33.0")) as run:
            doctor._confiture_cli_version()
        assert run.call_args[0][0][0] == str(venv_bin / "confiture")


#: A list literal whose first element is the bare name, across lines too.
BARE_ARGV = re.compile(r"\[\s*\"confiture\"\s*,")


def test_no_command_starts_with_a_bare_confiture() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "fraisier").rglob("*.py"))
        if BARE_ARGV.search(path.read_text())
    ]
    assert not offenders, offenders


def test_the_pattern_matches_the_shapes_it_guards() -> None:
    assert BARE_ARGV.search('cmd = ["confiture", "migrate"]')
    assert BARE_ARGV.search('_run(\n    [\n        "confiture",\n        "build",')
    assert not BARE_ARGV.search("[confiture_executable(), 'build']")

"""Do the installed units run on the Python floor? (#435)

fraisier requires Python 3.14. A host whose tool venv was installed on 3.13
keeps working on the release it has, but a self-upgrade is pinned to the
running interpreter and refuses every release from this one on, by design. The
host looks healthy and is stuck; nothing else says so.

The check reads the venv each unit's ``ExecStart=`` binary lives in
(``pyvenv.cfg``), so it answers for the interpreter the *service* runs on, not
the one the doctor happens to be run with.
"""

from __future__ import annotations

import stat

import pytest

from fraisier.doctor import DOCTOR_CHECKS

NAME = "unit_interpreter"


@pytest.fixture
def unit_dir(tmp_path, monkeypatch):
    d = tmp_path / "systemd"
    d.mkdir()
    monkeypatch.setattr("fraisier.doctor.SYSTEMD_UNIT_DIR", d)
    return d


def _venv(root, version_line, name="fraisier-webhook"):
    """A tool venv whose ``pyvenv.cfg`` says *version_line*, entrypoint symlinked."""
    venv = root / "tools" / "fraisier"
    (venv / "bin").mkdir(parents=True)
    exe = venv / "bin" / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    (venv / "pyvenv.cfg").write_text(f"home = /usr/bin\n{version_line}\n")
    link = root / "local-bin" / name
    link.parent.mkdir(exist_ok=True)
    link.symlink_to(exe)
    return link


def _unit(unit_dir, exec_start, name="fraisier-api-webhook.service"):
    (unit_dir / name).write_text(f"[Service]\nExecStart={exec_start}\n")


def _run():
    return DOCTOR_CHECKS[NAME].fn(None)


def test_the_check_is_registered() -> None:
    assert NAME in DOCTOR_CHECKS


class TestOnTheFloor:
    @pytest.mark.parametrize(
        "line", ["version_info = 3.14.5", "version = 3.14.2", "version_info = 3.15.0"]
    )
    def test_a_3_14_venv_passes(self, unit_dir, tmp_path, line) -> None:
        _unit(unit_dir, _venv(tmp_path, line))
        assert _run().status == "pass"


class TestBelowIt:
    @pytest.mark.parametrize(
        "line",
        ["version_info = 3.13.11", "version = 3.12.4", "version_info = 3.11.0"],
    )
    def test_an_older_venv_warns_and_names_the_unit(
        self, unit_dir, tmp_path, line
    ) -> None:
        _unit(unit_dir, _venv(tmp_path, line))
        result = _run()
        assert result.status == "warn"
        assert "fraisier-api-webhook.service" in result.detail
        assert line.split("= ")[1] in result.detail

    def test_the_fix_names_the_manual_move_and_why_self_upgrade_cannot(
        self, unit_dir, tmp_path
    ) -> None:
        _unit(unit_dir, _venv(tmp_path, "version_info = 3.13.11"))
        hint = _run().fix_hint or ""
        assert "uv tool install --force --python 3.14 fraisier==" in hint
        assert "self-upgrade" in hint

    def test_one_old_venv_among_good_ones_is_still_reported(
        self, unit_dir, tmp_path
    ) -> None:
        _unit(unit_dir, _venv(tmp_path / "a", "version_info = 3.14.5"))
        old = _venv(tmp_path / "b", "version_info = 3.13.11", name="fraisier-other")
        _unit(unit_dir, old, name="fraisier-api-other.service")
        result = _run()
        assert result.status == "warn"
        assert "fraisier-api-other.service" in result.detail
        assert "fraisier-api-webhook.service" not in result.detail


class TestWhatItCannotKnowIsASkip:
    def test_no_units_is_a_skip(self, unit_dir) -> None:
        assert _run().status == "skip"

    def test_a_venv_without_a_readable_version_is_a_skip_not_a_pass(
        self, unit_dir, tmp_path
    ) -> None:
        _unit(unit_dir, _venv(tmp_path, "# no version here"))
        assert _run().status == "skip"

    def test_a_binary_outside_any_venv_is_a_skip_not_a_pass(
        self, unit_dir, tmp_path
    ) -> None:
        loose = tmp_path / "fraisier-webhook"
        loose.write_text("#!/bin/sh\n")
        _unit(unit_dir, loose)
        assert _run().status == "skip"


class TestThePythonVersionCheckFollowsTheFloor:
    def test_the_running_interpreter_is_held_to_3_14(self, monkeypatch) -> None:
        monkeypatch.setattr("fraisier.doctor.sys.version_info", (3, 13, 11, "final", 0))
        result = DOCTOR_CHECKS["python_version"].fn(None)
        assert result.status == "fail"
        assert "3.14" in (result.fix_hint or "")

    def test_3_14_passes(self, monkeypatch) -> None:
        monkeypatch.setattr("fraisier.doctor.sys.version_info", (3, 14, 0, "final", 0))
        assert DOCTOR_CHECKS["python_version"].fn(None).status == "pass"


def test_doctor_md_covers_the_check() -> None:
    from pathlib import Path

    doc = (Path(__file__).resolve().parent.parent / "docs" / "doctor.md").read_text()
    assert NAME in doc

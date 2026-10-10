"""Doctor reports a 3.14 unit that still accepts a PEP 768 attach (#436).

What counts is the **effective** environment: the unit, then its
``<unit>.d/*.conf`` drop-ins in lexical order, with ``EnvironmentFile=`` and
``UnsetEnvironment=``. The documented way to lift the opt-out is a drop-in, so
a check reading only the unit file would call a forgotten lift fine.

CPython 3.14.5 disables attach for **any** value, empty included; only an
unset variable allows it (probed: unset, ``''``, ``0``, ``1``, ``no``,
``false``). So "disabled" here means "set".
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from fraisier import doctor

if TYPE_CHECKING:
    from fraisier.doctor import CheckResult

CHECK = "remote_debug_disabled"


@pytest.fixture
def unit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    units = tmp_path / "systemd"
    units.mkdir()
    monkeypatch.setattr(doctor, "SYSTEMD_UNIT_DIR", units)
    return units


def _venv(tmp_path: Path, version: str) -> Path:
    """A tool venv whose ``pyvenv.cfg`` says *version*; returns its fraisier."""
    venv = tmp_path / f"venv-{version}"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion_info = {version}\n")
    binary = venv / "bin" / "fraisier"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def _unit(unit_dir: Path, binary: Path, *env_lines: str, name: str = "w.service"):
    body = "\n".join(
        [
            "[Unit]",
            "Description=w",
            "",
            "[Service]",
            f"ExecStart={binary} webhook",
            *env_lines,
        ]
    )
    (unit_dir / name).write_text(body + "\n")


def _dropin(unit_dir: Path, unit: str, conf: str, *lines: str) -> Path:
    d = unit_dir / f"{unit}.d"
    d.mkdir(exist_ok=True)
    path = d / conf
    path.write_text("\n".join(["[Service]", *lines]) + "\n")
    return path


def _run() -> CheckResult:
    return doctor.DOCTOR_CHECKS[CHECK].fn(None)


def test_registered() -> None:
    assert CHECK in doctor.DOCTOR_CHECKS


def test_a_314_unit_without_it_warns(tmp_path: Path, unit_dir: Path) -> None:
    _unit(unit_dir, _venv(tmp_path, "3.14.5"))
    result = _run()
    assert result.status == "warn"
    assert "w.service" in result.detail
    assert result.fix_hint is not None
    assert "scaffold-install" in result.fix_hint


def test_a_314_unit_with_it_passes(tmp_path: Path, unit_dir: Path) -> None:
    _unit(
        unit_dir,
        _venv(tmp_path, "3.14.5"),
        "Environment=PYTHON_DISABLE_REMOTE_DEBUG=1",
    )
    assert _run().status == "pass"


@pytest.mark.parametrize("value", ["0", "", "no"])
def test_any_value_disables(tmp_path: Path, unit_dir: Path, value: str) -> None:
    """Probed on 3.14.5: CPython checks presence, not truthiness."""
    _unit(
        unit_dir,
        _venv(tmp_path, "3.14.5"),
        f"Environment=PYTHON_DISABLE_REMOTE_DEBUG={value}",
    )
    assert _run().status == "pass"


def test_a_dropin_that_unsets_it_warns_and_is_named(
    tmp_path: Path, unit_dir: Path
) -> None:
    _unit(
        unit_dir,
        _venv(tmp_path, "3.14.5"),
        "Environment=PYTHON_DISABLE_REMOTE_DEBUG=1",
    )
    dropin = _dropin(
        unit_dir,
        "w.service",
        "override.conf",
        "UnsetEnvironment=PYTHON_DISABLE_REMOTE_DEBUG",
    )
    result = _run()
    assert result.status == "warn"
    assert str(dropin) in result.detail


def test_a_dropin_resetting_environment_warns_and_is_named(
    tmp_path: Path, unit_dir: Path
) -> None:
    """An empty ``Environment=`` clears every assignment before it."""
    _unit(
        unit_dir,
        _venv(tmp_path, "3.14.5"),
        "Environment=PYTHON_DISABLE_REMOTE_DEBUG=1",
    )
    dropin = _dropin(unit_dir, "w.service", "10-reset.conf", "Environment=")
    result = _run()
    assert result.status == "warn"
    assert str(dropin) in result.detail


def test_dropins_apply_in_lexical_order(tmp_path: Path, unit_dir: Path) -> None:
    _unit(unit_dir, _venv(tmp_path, "3.14.5"))
    _dropin(
        unit_dir,
        "w.service",
        "20-set.conf",
        "Environment=PYTHON_DISABLE_REMOTE_DEBUG=1",
    )
    _dropin(unit_dir, "w.service", "10-reset.conf", "Environment=")
    assert _run().status == "pass"


def test_an_environment_file_counts(tmp_path: Path, unit_dir: Path) -> None:
    env_file = tmp_path / "w.env"
    env_file.write_text("# comment\nPYTHON_DISABLE_REMOTE_DEBUG=1\n")
    _unit(unit_dir, _venv(tmp_path, "3.14.5"), f"EnvironmentFile=-{env_file}")
    assert _run().status == "pass"


def test_several_assignments_on_one_line_count(tmp_path: Path, unit_dir: Path) -> None:
    _unit(
        unit_dir,
        _venv(tmp_path, "3.14.5"),
        'Environment="A=1 2" PYTHON_DISABLE_REMOTE_DEBUG=1',
    )
    assert _run().status == "pass"


def test_below_314_is_not_judged(tmp_path: Path, unit_dir: Path) -> None:
    _unit(unit_dir, _venv(tmp_path, "3.13.2"))
    assert _run().status == "skip"


def test_a_non_fraisier_unit_is_not_judged(tmp_path: Path, unit_dir: Path) -> None:
    venv = _venv(tmp_path, "3.14.5").parent
    other = venv / "gunicorn"
    other.write_text("#!/bin/sh\n")
    _unit(unit_dir, other)
    assert _run().status == "skip"


def test_a_serving_fraises_app_unit_is_judged(tmp_path: Path, unit_dir: Path) -> None:
    """The app's own interpreter opts out too (D3), found by its unit name."""
    from fraisier.config import FraisierConfig

    uvicorn = _venv(tmp_path, "3.14.5").parent / "uvicorn"
    uvicorn.write_text("#!/bin/sh\n")
    _unit(unit_dir, uvicorn, name="proj_api_production.service")
    _unit(unit_dir, uvicorn, name="unrelated.service")
    config_path = tmp_path / "fraises.yaml"
    config_path.write_text(
        "name: proj\nfraises:\n  api:\n    type: api\n    environments:\n"
        "      production:\n        app_path: /srv/api\n"
    )

    result = doctor.DOCTOR_CHECKS[CHECK].fn(FraisierConfig(config_path))

    assert result.status == "warn"
    assert "proj_api_production.service" in result.detail
    assert "unrelated.service" not in result.detail


def test_skips_when_the_unit_dir_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "SYSTEMD_UNIT_DIR", tmp_path / "missing")
    assert _run().status == "skip"


def test_doctor_md_covers_the_check() -> None:
    doc = (Path(__file__).resolve().parent.parent / "docs" / "doctor.md").read_text()
    assert f"| `{CHECK}` |" in doc

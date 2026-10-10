"""Doctor names the app units fraisier no longer renders (#432).

Before #432 the scaffold rendered a uvicorn unit for every fraise, scheduled
and backup ones included, and ``fraisier setup`` copied and enabled each of
them. Nothing renders them any more, so nothing tracks them either: an enabled
one still binds port 8000 at boot. Doctor reports them by name; nothing
deletes a unit on a host.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from fraisier import doctor
from fraisier.config import FraisierConfig

if TYPE_CHECKING:
    from fraisier.doctor import CheckResult

CHECK = "stale_app_units"

_CONFIG = """\
name: proj
fraises:
  api:
    type: api
    environments:
      production:
        app_path: /var/www/api
  nightly:
    type: scheduled
    environments:
      production:
        app_path: /var/www/api
  dumps:
    type: backup
    environments:
      production:
        app_path: /var/www/api
        systemd_service: dumps-legacy.service
  timer_shaped:
    type: scheduled
    environments:
      production:
        app_path: /var/www/api
        systemd_service: timer-shaped.service
        systemd_timer: timer-shaped.timer
"""


def _rendered_app_unit(fraise: str, env: str) -> str:
    """What ``core/service.j2`` wrote for a fraise, before #432 and since v0.3.0."""
    return (
        "[Unit]\n"
        f"Description={fraise} ({env})\n"
        "After=network.target\n\n"
        "[Service]\n"
        f"ExecStart=/var/www/api/.venv/bin/uvicorn {fraise}.main:app --port 8000\n"
    )


@pytest.fixture
def config(tmp_path: Path) -> FraisierConfig:
    path = tmp_path / "fraises.yaml"
    path.write_text(_CONFIG)
    return FraisierConfig(path)


@pytest.fixture
def unit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    units = tmp_path / "systemd"
    units.mkdir()
    monkeypatch.setattr(doctor, "SYSTEMD_UNIT_DIR", units)
    return units


def _install(unit_dir: Path, name: str, text: str, *, enabled: bool = False) -> None:
    (unit_dir / name).write_text(text)
    if enabled:
        wants = unit_dir / "multi-user.target.wants"
        wants.mkdir(exist_ok=True)
        (wants / name).symlink_to(unit_dir / name)


def _run(config: FraisierConfig | None) -> CheckResult:
    return doctor.DOCTOR_CHECKS[CHECK].fn(config)


def test_registered() -> None:
    assert CHECK in doctor.DOCTOR_CHECKS


def test_names_both_orphans_and_not_the_serving_unit(
    config: FraisierConfig, unit_dir: Path
) -> None:
    _install(
        unit_dir, "proj_api_production.service", _rendered_app_unit("api", "production")
    )
    _install(
        unit_dir,
        "proj_nightly_production.service",
        _rendered_app_unit("nightly", "production"),
        enabled=True,
    )
    _install(
        unit_dir, "dumps-legacy.service", _rendered_app_unit("dumps", "production")
    )

    result = _run(config)

    assert result.status == "warn"
    assert "proj_nightly_production.service (enabled)" in result.detail
    assert "dumps-legacy.service (disabled)" in result.detail
    assert "proj_api_production" not in result.detail
    assert result.fix_hint is not None
    assert (
        "sudo systemctl disable --now proj_nightly_production.service"
        in result.fix_hint
    )
    assert f"sudo rm {unit_dir / 'dumps-legacy.service'}" in result.fix_hint
    assert result.fix_hint.rstrip().endswith("sudo systemctl daemon-reload")


def test_a_unit_fraisier_did_not_write_is_left_alone(
    config: FraisierConfig, unit_dir: Path
) -> None:
    """A scheduled fraise's own ``systemd_service`` shares the name, not the body."""
    _install(
        unit_dir,
        "timer-shaped.service",
        "[Unit]\nDescription=Nightly job\n\n[Service]\nType=oneshot\n",
    )
    assert _run(config).status == "pass"


def test_a_host_without_orphans_passes(config: FraisierConfig, unit_dir: Path) -> None:
    _install(
        unit_dir, "proj_api_production.service", _rendered_app_unit("api", "production")
    )
    assert _run(config).status == "pass"


def test_skips_when_the_unit_dir_cannot_be_read(
    config: FraisierConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "SYSTEMD_UNIT_DIR", tmp_path / "missing")
    assert _run(config).status == "skip"


def test_skips_without_a_config(unit_dir: Path) -> None:
    assert _run(None).status == "skip"


def test_doctor_md_covers_the_check() -> None:
    doc = (Path(__file__).resolve().parent.parent / "docs" / "doctor.md").read_text()
    assert f"| `{CHECK}` |" in doc

"""The systemctl-helper allows every timer a deployer can act on (#447).

``ScheduledDeployer`` sends ``enable``/``start`` for its ``systemd_timer`` to the
root systemctl-helper, which refuses any unit outside the allowlist baked at
scaffold time. The allowlist used to walk ``jobs.*`` only, and only for
``type: scheduled``, so an env-level timer (the flat shape) and a backup job's
timer were refused at deploy time.

Asserted as an invariant over the deployer the registry builds, not shape by
shape, so a new shape cannot slip past.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fraisier.config import FraisierConfig
from fraisier.deployers.registry import build_deployer
from fraisier.deployers.scheduled import ScheduledDeployer
from fraisier.runners import LocalRunner
from fraisier.scaffold.renderer import ScaffoldRenderer

_REPO = Path(__file__).resolve().parent.parent
_PRINTOPTIM = Path.home() / "code" / "printoptim_backend" / "fraises.yaml"

# Every way a scheduled/backup fraise can name a timer: env level (the flat
# shape) and per job, for both types.
_SHAPES = """\
name: shapes
fraises:
  nightly:
    type: scheduled
    environments:
      production:
        app_path: /var/www/nightly
        systemd_service: nightly.service
        systemd_timer: nightly.timer
  stats:
    type: scheduled
    environments:
      production:
        app_path: /var/www/stats
        jobs:
          daily:
            systemd_service: stats-daily.service
            systemd_timer: stats-daily.timer
  dumps:
    type: backup
    environments:
      production:
        app_path: /var/www/dumps
        systemd_service: dumps.service
        systemd_timer: dumps.timer
  offsite:
    type: backup
    environments:
      production:
        app_path: /var/www/offsite
        jobs:
          sync:
            systemd_service: offsite-sync.service
            systemd_timer: offsite-sync.timer
"""


def _deployer_timers(config: FraisierConfig) -> list[tuple[str, str]]:
    """``(where, timer)`` for every deployer the registry can build."""
    timers: list[tuple[str, str]] = []
    for name in config.list_fraises():
        fraise = config.get_fraise(name) or {}
        fraise_type = fraise.get("type")
        for env_name in fraise.get("environments") or {}:
            merged = config.get_fraise_environment(name, env_name) or {}
            jobs = list((merged.get("jobs") or {}).keys())
            for job in [None, *jobs]:
                deployer = build_deployer(
                    fraise_type, merged, runner=LocalRunner(), job=job
                )
                if isinstance(deployer, ScheduledDeployer) and deployer.systemd_timer:
                    where = f"{name}/{env_name}" + (f"/{job}" if job else "")
                    timers.append((where, deployer.systemd_timer))
    return timers


def _assert_every_timer_is_allowed(config_path: Path) -> int:
    config = FraisierConfig(str(config_path))
    allowed = set(ScaffoldRenderer(config).context["allowed_services"])
    timers = _deployer_timers(config)
    refused = [(where, timer) for where, timer in timers if timer not in allowed]
    assert refused == [], f"{config_path}: the helper would refuse {refused}"
    return len(timers)


def _real_configs() -> list[Path]:
    paths = [
        _REPO / "fraises.yaml",
        _REPO / "fraises.example.yaml",
        *sorted((_REPO / "examples").glob("*/fraises.yaml")),
    ]
    if _PRINTOPTIM.is_file():
        paths.append(_PRINTOPTIM)
    return [p for p in paths if p.is_file()]


@pytest.mark.parametrize("config_path", _real_configs(), ids=str)
def test_every_timer_in_a_real_config_is_allowed(config_path: Path) -> None:
    _assert_every_timer_is_allowed(config_path)


def test_every_timer_shape_is_allowed(tmp_path: Path) -> None:
    path = tmp_path / "fraises.yaml"
    path.write_text(_SHAPES)
    assert _assert_every_timer_is_allowed(path) == 4


def _allowed(fraise: dict[str, Any]) -> list[str]:
    from fraisier.scaffold.renderer import _collect_allowed_services

    return _collect_allowed_services("p", [{"name": "f", **fraise}])


@pytest.mark.parametrize("fraise_type", ["scheduled", "backup"])
def test_an_env_level_service_is_allowed_beside_its_timer(fraise_type: str) -> None:
    env = {
        "app_path": "/srv",
        "systemd_service": "j.service",
        "systemd_timer": "j.timer",
    }
    allowed = _allowed({"type": fraise_type, "environments": {"production": env}})
    assert {"j.service", "j.timer"} <= set(allowed)


def test_an_etl_jobs_block_is_not_walked() -> None:
    """Only the two types the registry builds a ``ScheduledDeployer`` for."""
    env = {"app_path": "/srv", "jobs": {"x": {"systemd_timer": "x.timer"}}}
    assert "x.timer" not in _allowed({"type": "etl", "environments": {"e": env}})

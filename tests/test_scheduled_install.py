"""Tests for fraisier.scheduled_install — the `fraisier scheduled-install` command's pure layers."""

from __future__ import annotations

from pathlib import Path

import pytest

from fraisier.config import FraisierConfig
from fraisier.scheduled_install import (
    ScheduledUnitInstall,
    enumerate_scheduled_units,
)


def _write_scheduled_yaml(tmp_path: Path) -> Path:
    """Minimal fraises.yaml with one type:scheduled fraise + one job declaring
    both a systemd_service and a systemd_timer.
    """
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        """
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  alerter:
    type: scheduled
    description: Long-lock alerter
    environments:
      production:
        app_path: /var/www/api.printoptim.io
        jobs:
          poll:
            name: printoptim-long-lock-alerter
            description: Polls pg_locks every minute
            systemd_service: printoptim-long-lock-alerter.service
            systemd_timer: printoptim-long-lock-alerter.timer
            schedule: "*-*-* *:*:00"
"""
    )
    return cfg


def test_enumerate_scheduled_units_yields_service_and_timer(tmp_path):
    """A type:scheduled fraise with one job declaring both a service and a timer
    yields exactly two ScheduledUnitInstall rows pointing at the consumer's
    scripts/systemd/ directory."""
    cfg = _write_scheduled_yaml(tmp_path)
    config = FraisierConfig(str(cfg))

    units = enumerate_scheduled_units(config, environment="production")

    assert len(units) == 2, f"expected service + timer, got {units!r}"
    assert {u.unit_name for u in units} == {
        "printoptim-long-lock-alerter.service",
        "printoptim-long-lock-alerter.timer",
    }

    by_name = {u.unit_name: u for u in units}
    timer = by_name["printoptim-long-lock-alerter.timer"]
    service = by_name["printoptim-long-lock-alerter.service"]

    assert timer.is_timer is True
    assert service.is_timer is False

    assert timer.fraise_name == "alerter"
    assert timer.environment == "production"
    assert timer.job_name == "poll"

    assert timer.source_path == Path(
        "/var/www/api.printoptim.io/scripts/systemd/printoptim-long-lock-alerter.timer"
    )
    assert timer.dest_path == Path(
        "/etc/systemd/system/printoptim-long-lock-alerter.timer"
    )
    assert isinstance(timer, ScheduledUnitInstall)


def test_enumerate_skips_non_scheduled_fraise_with_jobs(tmp_path):
    """A type:backup fraise (which also uses the jobs.* substructure per
    list_all_deployments) must NOT be enumerated — only type:scheduled."""
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        """
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  nightly_backup:
    type: backup
    environments:
      production:
        app_path: /var/www/api
        jobs:
          dump:
            name: api-nightly-backup
            systemd_service: api-nightly-backup.service
            systemd_timer: api-nightly-backup.timer
            schedule: "0 3 * * *"
"""
    )
    config = FraisierConfig(str(cfg))

    units = enumerate_scheduled_units(config, environment="production")

    assert units == []


def test_enumerate_job_without_timer_yields_only_service(tmp_path):
    """A scheduled job that omits systemd_timer (one-shot service, not a timer)
    yields just the .service row."""
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        """
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  one_shot:
    type: scheduled
    environments:
      production:
        app_path: /var/www/api
        jobs:
          run:
            name: api-one-shot
            systemd_service: api-one-shot.service
            schedule: "*-*-* *:*:00"
"""
    )
    config = FraisierConfig(str(cfg))

    units = enumerate_scheduled_units(config, environment="production")

    assert len(units) == 1
    assert units[0].unit_name == "api-one-shot.service"
    assert units[0].is_timer is False


def test_enumerate_unknown_environment_returns_empty_list(tmp_path):
    """Asking for an env the fraise doesn't declare returns [] — no exception."""
    cfg = _write_scheduled_yaml(tmp_path)
    config = FraisierConfig(str(cfg))

    units = enumerate_scheduled_units(config, environment="staging")

    assert units == []


def test_enumerate_invalid_unit_name_raises(tmp_path):
    """A systemd_service/systemd_timer that fails validate_service_name must
    raise loudly. Silent skipping would hide config bugs.

    Note: validate_service_name only enforces a charset; it accepts ``..``
    (Phase 03 adds the path-traversal guards that catch that). Here we use a
    name with ``/`` which the validator does reject.
    """
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        """
git:
  provider: github
  github:
    webhook_secret: test-secret

fraises:
  bad:
    type: scheduled
    environments:
      production:
        app_path: /var/www/api
        jobs:
          poll:
            name: bad
            systemd_service: "bad/name.service"
            schedule: "*-*-* *:*:00"
"""
    )
    config = FraisierConfig(str(cfg))

    with pytest.raises(ValueError, match="Invalid service name"):
        enumerate_scheduled_units(config, environment="production")

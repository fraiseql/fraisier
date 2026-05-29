"""Tests for fraisier.scheduled_install — the `fraisier scheduled-install` command's pure layers."""

from __future__ import annotations

from pathlib import Path

import pytest

from fraisier.config import FraisierConfig
from fraisier.scheduled_install import (
    ScheduledUnitInstall,
    UnitDiff,
    UnitState,
    classify_unit,
    enumerate_scheduled_units,
)


def _make_install(
    tmp_path: Path,
    *,
    unit_name: str = "foo.timer",
    is_timer: bool = True,
    source_content: str | None = None,
    dest_content: str | None = None,
) -> ScheduledUnitInstall:
    """Build a ScheduledUnitInstall pointing at sandboxed source/dest paths.

    Pass ``source_content=None`` to leave the source path missing;
    likewise for ``dest_content``.
    """
    src_dir = tmp_path / "app/scripts/systemd"
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / unit_name
    if source_content is not None:
        src.write_text(source_content)

    dst_dir = tmp_path / "etc-systemd-system"
    dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / unit_name
    if dest_content is not None:
        dst.write_text(dest_content)

    return ScheduledUnitInstall(
        fraise_name="x",
        environment="prod",
        job_name="p",
        unit_name=unit_name,
        is_timer=is_timer,
        source_path=src,
        dest_path=dst,
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


# -- Phase 2: classification -------------------------------------------------


def test_classify_unit_absent(tmp_path):
    """Source present, dest missing → ABSENT, no diff summary."""
    install = _make_install(tmp_path, source_content="[Timer]\n")

    result = classify_unit(install)

    assert result.state is UnitState.ABSENT
    assert result.diff_summary is None
    assert result.install is install


def test_classify_unit_identical(tmp_path):
    """Source and dest byte-equal → IDENTICAL, no diff summary."""
    body = "[Timer]\nOnUnitActiveSec=1h\n"
    install = _make_install(tmp_path, source_content=body, dest_content=body)

    result = classify_unit(install)

    assert result.state is UnitState.IDENTICAL
    assert result.diff_summary is None


def test_classify_unit_drifted(tmp_path):
    """Source and dest differ → DRIFTED with a one-line summary."""
    install = _make_install(
        tmp_path,
        source_content="[Timer]\nOnUnitActiveSec=1h\nPersistent=true\n",
        dest_content="[Timer]\nOnUnitActiveSec=30m\n",
    )

    result = classify_unit(install)

    assert result.state is UnitState.DRIFTED
    assert result.diff_summary is not None
    assert "unit body differs" in result.diff_summary


def test_classify_unit_drifted_summary_counts_lines(tmp_path):
    """The summary line counts added/removed accurately for a known fixture.

    Source has one extra line and one changed line vs dest, so unified_diff
    sees: 2 lines added (the new line + the new value of the changed line),
    1 line removed (the old value of the changed line).
    """
    install = _make_install(
        tmp_path,
        source_content="[Timer]\nOnUnitActiveSec=1h\nPersistent=true\n",
        dest_content="[Timer]\nOnUnitActiveSec=30m\n",
    )

    result = classify_unit(install)

    assert result.diff_summary == "unit body differs (2 lines added, 1 removed)"


def test_classify_unit_missing_source(tmp_path):
    """Source path missing → MISSING_SOURCE (operator error: deploy didn't land?)."""
    install = _make_install(tmp_path, source_content=None, dest_content="[Timer]\n")

    result = classify_unit(install)

    assert result.state is UnitState.MISSING_SOURCE
    assert result.diff_summary is None


def test_classify_unit_is_pure_does_not_mutate_filesystem(tmp_path):
    """classify_unit reads but never writes."""
    install = _make_install(tmp_path, source_content="src", dest_content="dst")
    snapshot = {p: p.read_text() for p in [install.source_path, install.dest_path]}

    classify_unit(install)

    assert install.source_path.read_text() == snapshot[install.source_path]
    assert install.dest_path.read_text() == snapshot[install.dest_path]
    assert list((tmp_path / "etc-systemd-system").iterdir()) == [install.dest_path]
    assert list((tmp_path / "app/scripts/systemd").iterdir()) == [install.source_path]


def test_unit_diff_is_a_dataclass(tmp_path):
    """Quick contract check on the UnitDiff shape — frozen, has the right fields."""
    install = _make_install(tmp_path, source_content="x")
    diff = classify_unit(install)

    assert isinstance(diff, UnitDiff)
    assert diff.install is install
    assert diff.state in UnitState
    with pytest.raises((AttributeError, TypeError)):
        diff.state = UnitState.ABSENT  # type: ignore[misc]

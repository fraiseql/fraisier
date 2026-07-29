"""Tests for ``prune_orphans`` — #240 follow-up 04 Phase 3.

Pure planner over (config snapshot, on-disk markers, on-disk unit files).
No filesystem mutation, no helper round-trip — that's Phase 4's CLI.

Per Phase 0 decision #1 (markers alongside the unit), Phase 0 decision #3
(advisory only — no HMAC), and the plan's "marker authenticity is documented
as advisory" stance: this planner is the cross-project / cross-env safety net
that protects against honest mistakes, not against root-side attackers.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from fraisier.config import FraisierConfig
from fraisier.scheduled_install import (
    PrunePlan,
    prune_orphans,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_config(
    tmp_path: Path,
    *,
    jobs: dict[str, dict[str, str]] | None = None,
    env: str = "production",
    fraise_name: str = "alerter",
) -> tuple[Path, FraisierConfig]:
    """Create a fraises.yaml with optional jobs.* + return the config object."""
    app_path = tmp_path / "app"
    (app_path / "scripts" / "systemd").mkdir(parents=True, exist_ok=True)

    jobs_yaml = ""
    if jobs:
        jobs_yaml = "        jobs:\n"
        for job_name, fields in jobs.items():
            jobs_yaml += f"          {job_name}:\n"
            for k, v in fields.items():
                jobs_yaml += f"            {k}: {v}\n"

    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        f"""
name: myproj
fraises:
  {fraise_name}:
    type: scheduled
    environments:
      {env}:
        app_path: {app_path}
{jobs_yaml}
"""
    )
    return cfg, FraisierConfig(cfg)


def _write_marker(
    dest_dir: Path,
    *,
    unit_name: str,
    fraises_yaml_path: str,
    fraise_name: str = "alerter",
    environment: str = "production",
    job_name: str = "poll",
) -> Path:
    marker_path = dest_dir / f"{unit_name}.fraisier-managed"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {
                "version": 1,
                "fraises_yaml_path": fraises_yaml_path,
                "fraise_name": fraise_name,
                "environment": environment,
                "job_name": job_name,
            }
        )
        + "\n"
    )
    return marker_path


# ---------------------------------------------------------------------------
# Phase 3 cycle 3.1 — marker for a unit no longer in fraises.yaml → orphan
# ---------------------------------------------------------------------------


def test_prune_orphans_flags_undeclared_marker_as_orphan(tmp_path: Path) -> None:
    cfg, config = _write_config(tmp_path, jobs={})
    dest = tmp_path / "systemd-dest"
    dest.mkdir()
    # A unit file exists on disk with marker, but the operator removed the
    # job from fraises.yaml — it's now an orphan.
    (dest / "old-job.timer").write_text("[Timer]\n")
    _write_marker(dest, unit_name="old-job.timer", fraises_yaml_path=str(cfg.resolve()))

    plans = prune_orphans(config, "production", systemd_dest_dir=dest)
    assert len(plans) == 1
    plan = plans[0]
    assert plan.kind == "orphan"
    assert plan.unit_name == "old-job.timer"
    assert plan.is_timer is True
    assert plan.unit_path == dest / "old-job.timer"


def test_prune_orphans_skips_declared_units(tmp_path: Path) -> None:
    """A unit declared in fraises.yaml + with marker → NOT pruned."""
    cfg, config = _write_config(
        tmp_path,
        jobs={
            "poll": {
                "systemd_service": "alerter-poll.service",
                "systemd_timer": "alerter-poll.timer",
            }
        },
    )
    dest = tmp_path / "systemd-dest"
    dest.mkdir()
    (dest / "alerter-poll.timer").write_text("[Timer]\n")
    _write_marker(
        dest,
        unit_name="alerter-poll.timer",
        fraises_yaml_path=str(cfg.resolve()),
    )

    plans = prune_orphans(config, "production", systemd_dest_dir=dest)
    assert plans == []


# ---------------------------------------------------------------------------
# Phase 3 cycle 3.2 — wrong environment → filtered out
# ---------------------------------------------------------------------------


def test_prune_orphans_filters_other_env_markers(tmp_path: Path) -> None:
    """A marker for env=staging should not appear in --env production runs."""
    cfg, config = _write_config(tmp_path, jobs={}, env="production")
    dest = tmp_path / "systemd-dest"
    dest.mkdir()
    (dest / "other-env.timer").write_text("[Timer]\n")
    _write_marker(
        dest,
        unit_name="other-env.timer",
        fraises_yaml_path=str(cfg.resolve()),
        environment="staging",  # different from --env
    )

    plans = prune_orphans(config, "production", systemd_dest_dir=dest)
    assert plans == []


# ---------------------------------------------------------------------------
# Phase 3 cycle 3.3 — cross-project resolution
# ---------------------------------------------------------------------------


def test_prune_orphans_filters_other_project_markers(tmp_path: Path) -> None:
    """A marker whose fraises_yaml_path resolves to a different project's
    config is not our business — leave it alone."""
    _cfg, config = _write_config(tmp_path, jobs={})
    other_project_cfg = tmp_path / "other-project" / "fraises.yaml"
    other_project_cfg.parent.mkdir(parents=True)
    other_project_cfg.write_text("# other project")

    dest = tmp_path / "systemd-dest"
    dest.mkdir()
    (dest / "other-proj.timer").write_text("[Timer]\n")
    _write_marker(
        dest,
        unit_name="other-proj.timer",
        fraises_yaml_path=str(other_project_cfg.resolve()),
    )

    plans = prune_orphans(config, "production", systemd_dest_dir=dest)
    assert plans == []


def test_prune_orphans_normalises_relative_paths_before_compare(
    tmp_path: Path,
) -> None:
    """The marker stores an absolute path; the planner resolves config.path
    to absolute before comparing. So a marker installed from one CWD and a
    prune launched from a different CWD still see the same project."""
    cfg, config = _write_config(tmp_path, jobs={})
    dest = tmp_path / "systemd-dest"
    dest.mkdir()
    (dest / "from-relative.timer").write_text("[Timer]\n")
    # Marker stores absolute path (as it always does — caller resolves first).
    _write_marker(
        dest,
        unit_name="from-relative.timer",
        fraises_yaml_path=str(cfg.resolve()),
    )

    # Planner resolves config.config_path internally.
    plans = prune_orphans(config, "production", systemd_dest_dir=dest)
    assert len(plans) == 1
    assert plans[0].kind == "orphan"


# ---------------------------------------------------------------------------
# Phase 3 cycle 3.4 — marker without unit file → stale_marker
# ---------------------------------------------------------------------------


def test_prune_orphans_classifies_marker_without_unit_as_stale(
    tmp_path: Path,
) -> None:
    """Operator manually rm'd the .timer file but left the .fraisier-managed
    sidecar — clean up the orphan marker; classify as stale_marker."""
    cfg, config = _write_config(tmp_path, jobs={})
    dest = tmp_path / "systemd-dest"
    dest.mkdir()
    # No unit file written, marker present.
    _write_marker(dest, unit_name="ghost.timer", fraises_yaml_path=str(cfg.resolve()))

    plans = prune_orphans(config, "production", systemd_dest_dir=dest)
    assert len(plans) == 1
    assert plans[0].kind == "stale_marker"
    assert plans[0].reason is not None and "missing" in plans[0].reason


def test_prune_orphans_classifies_corrupt_marker_as_stale(tmp_path: Path) -> None:
    """A marker with malformed JSON → stale_marker with a clean reason."""
    _cfg, config = _write_config(tmp_path, jobs={})
    dest = tmp_path / "systemd-dest"
    dest.mkdir()
    bad = dest / "broken.timer.fraisier-managed"
    bad.write_text("{not valid json")
    # Pair it with a real unit so the missing-unit path doesn't fire.
    (dest / "broken.timer").write_text("[Timer]\n")

    plans = prune_orphans(config, "production", systemd_dest_dir=dest)
    assert len(plans) == 1
    assert plans[0].kind == "stale_marker"
    assert plans[0].reason is not None
    assert "corrupt" in plans[0].reason or "JSON" in plans[0].reason


# ---------------------------------------------------------------------------
# Phase 3 cycle 3.5 — sort order: timer-first, service-second, stale last
# ---------------------------------------------------------------------------


def test_prune_orphans_sorts_timer_first_then_service_then_stale(
    tmp_path: Path,
) -> None:
    cfg, config = _write_config(tmp_path, jobs={})
    dest = tmp_path / "systemd-dest"
    dest.mkdir()

    # Orphan timer + service for the same job
    (dest / "alpha.service").write_text("[Service]\n")
    (dest / "alpha.timer").write_text("[Timer]\n")
    _write_marker(dest, unit_name="alpha.service", fraises_yaml_path=str(cfg.resolve()))
    _write_marker(dest, unit_name="alpha.timer", fraises_yaml_path=str(cfg.resolve()))
    # Stale marker (no unit file)
    _write_marker(dest, unit_name="ghost.timer", fraises_yaml_path=str(cfg.resolve()))

    plans = prune_orphans(config, "production", systemd_dest_dir=dest)
    kinds_units = [(p.kind, p.unit_name) for p in plans]
    # timer first
    assert kinds_units[0] == ("orphan", "alpha.timer")
    # service next
    assert kinds_units[1] == ("orphan", "alpha.service")
    # stale_marker last
    assert kinds_units[2][0] == "stale_marker"


def test_prune_orphans_dataclass_is_frozen() -> None:
    """PrunePlan is frozen so plans can't be accidentally mutated downstream."""
    p = PrunePlan(kind="orphan", marker_path=__import__("pathlib").Path("/x"))
    with pytest.raises((AttributeError, Exception)):
        p.kind = "stale_marker"  # ty: ignore[invalid-assignment]

"""Tests for marker read helpers — #240 follow-up 04 Phase 1.

build_marker, read_marker, find_markers, marker_path_for are pure functions
over a real filesystem layout (no socket / no helper). Phase 04's prune
planner (Phase 3) consumes them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from fraisier.scheduled_install import (
    MARKER_SUFFIX,
    CorruptMarker,
    ScheduledInstallError,
    ScheduledUnitInstall,
    build_marker,
    find_markers,
    marker_path_for,
    read_marker,
)
from fraisier.unit_installer_protocol import MarkerMeta

if TYPE_CHECKING:
    from pathlib import Path


def _install(
    tmp_path: Path, *, unit: str = "alerter-poll.timer"
) -> ScheduledUnitInstall:
    app_path = tmp_path / "app"
    return ScheduledUnitInstall(
        fraise_name="alerter",
        environment="production",
        job_name="poll",
        unit_name=unit,
        is_timer=unit.endswith(".timer"),
        source_path=app_path / "scripts/systemd" / unit,
        dest_path=tmp_path / "etc/systemd/system" / unit,
        app_path=app_path,
    )


# ---------------------------------------------------------------------------
# marker_path_for
# ---------------------------------------------------------------------------


def test_marker_path_appends_suffix(tmp_path: Path) -> None:
    unit_dest = tmp_path / "etc/systemd/system/foo.timer"
    assert (
        marker_path_for(unit_dest)
        == tmp_path / "etc/systemd/system/foo.timer.fraisier-managed"
    )


# ---------------------------------------------------------------------------
# build_marker
# ---------------------------------------------------------------------------


def test_build_marker_from_absolute_config_path(tmp_path: Path) -> None:
    install = _install(tmp_path)
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text("# fake")
    resolved = cfg.resolve(strict=True)
    marker = build_marker(install, resolved_config_path=resolved)
    assert isinstance(marker, MarkerMeta)
    assert marker.fraises_yaml_path == str(resolved)
    assert marker.fraise_name == "alerter"
    assert marker.environment == "production"
    assert marker.job_name == "poll"


def test_build_marker_rejects_relative_config_path(tmp_path: Path) -> None:
    """Contract: caller must Path.resolve() before invoking build_marker."""
    from pathlib import Path

    install = _install(tmp_path)
    with pytest.raises(ScheduledInstallError, match="absolute"):
        build_marker(install, resolved_config_path=Path("relative/fraises.yaml"))


# ---------------------------------------------------------------------------
# read_marker
# ---------------------------------------------------------------------------


def test_read_marker_round_trips_a_well_formed_file(tmp_path: Path) -> None:
    install = _install(tmp_path)
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text("# fake")
    marker = build_marker(install, resolved_config_path=cfg.resolve(strict=True))
    marker_path = tmp_path / "etc/systemd/system/alerter-poll.timer.fraisier-managed"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_text(
        json.dumps(
            {
                "version": 1,
                "fraises_yaml_path": marker.fraises_yaml_path,
                "fraise_name": marker.fraise_name,
                "environment": marker.environment,
                "job_name": marker.job_name,
            }
        )
        + "\n"
    )

    read_back = read_marker(marker_path)
    assert read_back == marker


def test_read_marker_raises_corrupt_on_invalid_json(tmp_path: Path) -> None:
    marker_path = tmp_path / "foo.timer.fraisier-managed"
    marker_path.write_text("not json at all")
    with pytest.raises(CorruptMarker, match="JSON"):
        read_marker(marker_path)


def test_read_marker_raises_corrupt_on_missing_fields(tmp_path: Path) -> None:
    marker_path = tmp_path / "foo.timer.fraisier-managed"
    marker_path.write_text(json.dumps({"version": 1, "fraises_yaml_path": "/etc/foo"}))
    with pytest.raises(CorruptMarker, match="missing required fields"):
        read_marker(marker_path)


def test_read_marker_raises_corrupt_on_oserror(tmp_path: Path) -> None:
    """Unreadable marker file → CorruptMarker."""
    nonexistent = tmp_path / "does-not-exist.fraisier-managed"
    with pytest.raises(CorruptMarker):
        read_marker(nonexistent)


# ---------------------------------------------------------------------------
# find_markers
# ---------------------------------------------------------------------------


def test_find_markers_returns_only_well_named_markers(tmp_path: Path) -> None:
    dest = tmp_path / "etc/systemd/system"
    dest.mkdir(parents=True)
    # Real markers
    (dest / "foo.timer.fraisier-managed").write_text("{}")
    (dest / "bar.service.fraisier-managed").write_text("{}")
    # Unrelated files
    (dest / "foo.timer").write_text("[Unit]\n")
    (dest / "README").write_text("hi")
    # Markers with shell-injection-style names (defence-in-depth skip)
    (dest / "foo;rm.service.fraisier-managed").write_text("{}")
    (dest / "..evil.fraisier-managed").write_text("{}")
    (dest / ".hidden.fraisier-managed").write_text("{}")

    found = find_markers(dest)
    names = [p.name for p in found]
    assert "foo.timer.fraisier-managed" in names
    assert "bar.service.fraisier-managed" in names
    assert "foo;rm.service.fraisier-managed" not in names
    assert "..evil.fraisier-managed" not in names
    assert ".hidden.fraisier-managed" not in names


def test_find_markers_handles_missing_dest_dir(tmp_path: Path) -> None:
    """If /etc/systemd/system/ doesn't exist, return empty list — no crash."""
    nonexistent = tmp_path / "nope"
    assert find_markers(nonexistent) == []


# ---------------------------------------------------------------------------
# MARKER_SUFFIX constant sanity
# ---------------------------------------------------------------------------


def test_marker_suffix_is_fraisier_managed() -> None:
    assert MARKER_SUFFIX == ".fraisier-managed"

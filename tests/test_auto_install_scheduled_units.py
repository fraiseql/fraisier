"""Tests for ``auto_install_scheduled_units`` — #240 follow-up 01 Phase 2.

Drives the webhook hook end-to-end against a mocked
``apply_unit_diffs_via_helper``. The helper's manifest building + socket
exchange is already tested separately; here we pin policy semantics
(fail / overwrite / skip), busy retry budget, and pre-v0.29 detection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fraisier.config import FraisierConfig
from fraisier.scheduled_install import (
    ApplyReport,
    AutoInstallPolicy,
    ScheduledInstallError,
    ScheduledUnitInstall,
    UnitDiff,
    UnitState,
    auto_install_scheduled_units,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_config(tmp_path: Path) -> tuple[Path, FraisierConfig]:
    app_path = tmp_path / "app"
    (app_path / "scripts" / "systemd").mkdir(parents=True)
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text(
        f"""
name: myproj
fraises:
  alerter:
    type: scheduled
    environments:
      production:
        app_path: {app_path}
        jobs:
          poll:
            systemd_service: alerter-poll.service
            systemd_timer: alerter-poll.timer
"""
    )
    return cfg, FraisierConfig(cfg)


def _patch_apply(monkeypatch, *, results):
    """Patch apply_unit_diffs_via_helper to return successive canned results."""
    iter_results = iter(results)

    def fake(diffs, **kwargs):
        return next(iter_results)

    monkeypatch.setattr("fraisier.scheduled_install.apply_unit_diffs_via_helper", fake)
    monkeypatch.setattr(
        "fraisier.scheduled_install._validate_unit_path_safety",
        lambda *_a, **_k: None,
    )


def _absent_diff(tmp_path: Path) -> UnitDiff:
    app_path = tmp_path / "app"
    install = ScheduledUnitInstall(
        fraise_name="alerter",
        environment="production",
        job_name="poll",
        unit_name="alerter-poll.timer",
        is_timer=True,
        source_path=app_path / "scripts/systemd/alerter-poll.timer",
        dest_path=tmp_path / "etc/systemd/system/alerter-poll.timer",
        app_path=app_path,
    )
    return UnitDiff(install=install, state=UnitState.ABSENT, diff_summary=None)


# ---------------------------------------------------------------------------
# Pre-v0.29 detection (cycle 2.7)
# ---------------------------------------------------------------------------


def test_raises_actionable_error_when_helper_socket_absent(
    tmp_path: Path,
) -> None:
    """Pre-v0.29 host → ScheduledInstallError naming scaffold-install."""
    _cfg, config = _write_config(tmp_path)
    with pytest.raises(ScheduledInstallError, match="scaffold-install"):
        auto_install_scheduled_units(
            config,
            "production",
            fraise_name="alerter",
            policy=AutoInstallPolicy(),
            socket_path=tmp_path / "missing.sock",
            is_socket_present=False,
        )


def test_returns_empty_report_when_no_units_declared(tmp_path: Path) -> None:
    """No type:scheduled units for the fraise → empty report, no helper call."""
    _cfg, config = _write_config(tmp_path)
    report = auto_install_scheduled_units(
        config,
        "production",
        fraise_name="some-other-fraise",  # not in config
        policy=AutoInstallPolicy(),
        socket_path=tmp_path / "fake.sock",
        is_socket_present=True,
    )
    assert report.installed == ()


# ---------------------------------------------------------------------------
# Drift policy: fail (cycle 2.3 — locked default)
# ---------------------------------------------------------------------------


def test_drift_fail_raises_before_helper_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """on_drift=fail + any DRIFTED diff → raise, never invoke helper."""
    _cfg, config = _write_config(tmp_path)

    drifted = _absent_diff(tmp_path)
    drifted = UnitDiff(
        install=drifted.install, state=UnitState.DRIFTED, diff_summary="hi"
    )

    monkeypatch.setattr("fraisier.scheduled_install.classify_unit", lambda _u: drifted)

    called = []
    monkeypatch.setattr(
        "fraisier.scheduled_install.apply_unit_diffs_via_helper",
        lambda *_a, **_k: called.append("helper-called"),
    )

    with pytest.raises(ScheduledInstallError, match="drifted units"):
        auto_install_scheduled_units(
            config,
            "production",
            fraise_name="alerter",
            policy=AutoInstallPolicy(on_drift="fail"),
            socket_path=tmp_path / "fake.sock",
            is_socket_present=True,
        )
    assert called == []  # helper never invoked


# ---------------------------------------------------------------------------
# Drift policy: overwrite (cycle 2.4)
# ---------------------------------------------------------------------------


def test_drift_overwrite_records_units_and_calls_with_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cfg, config = _write_config(tmp_path)

    drifted = _absent_diff(tmp_path)
    drifted_diff = UnitDiff(
        install=drifted.install, state=UnitState.DRIFTED, diff_summary="hi"
    )
    monkeypatch.setattr(
        "fraisier.scheduled_install.classify_unit", lambda _u: drifted_diff
    )

    # kwargs.get returns None when the key is absent — the list holds that too.
    captured_force: list[bool | None] = []

    def fake_apply(diffs, **kwargs):
        captured_force.append(kwargs.get("force"))
        return ApplyReport(
            written=tuple(d.install for d in diffs),
            skipped_identical=(),
            enabled_timers=(),
            reloaded=True,
        )

    monkeypatch.setattr(
        "fraisier.scheduled_install.apply_unit_diffs_via_helper", fake_apply
    )

    report = auto_install_scheduled_units(
        config,
        "production",
        fraise_name="alerter",
        policy=AutoInstallPolicy(on_drift="overwrite"),
        socket_path=tmp_path / "fake.sock",
        is_socket_present=True,
    )

    assert captured_force == [True]
    assert "alerter-poll.timer" in report.drift_overwrites


# ---------------------------------------------------------------------------
# Drift policy: skip (cycle 2.5)
# ---------------------------------------------------------------------------


def test_drift_skip_filters_drifted_diffs_records_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cfg, config = _write_config(tmp_path)

    drifted = _absent_diff(tmp_path)
    drifted_diff = UnitDiff(
        install=drifted.install, state=UnitState.DRIFTED, diff_summary="hi"
    )
    monkeypatch.setattr(
        "fraisier.scheduled_install.classify_unit", lambda _u: drifted_diff
    )

    captured_diffs: list = []

    def fake_apply(diffs, **kwargs):
        captured_diffs.extend(diffs)
        return ApplyReport(
            written=(), skipped_identical=(), enabled_timers=(), reloaded=False
        )

    monkeypatch.setattr(
        "fraisier.scheduled_install.apply_unit_diffs_via_helper", fake_apply
    )

    report = auto_install_scheduled_units(
        config,
        "production",
        fraise_name="alerter",
        policy=AutoInstallPolicy(on_drift="skip"),
        socket_path=tmp_path / "fake.sock",
        is_socket_present=True,
    )

    # DRIFTED diffs were filtered out of the manifest.
    assert all(d.state is not UnitState.DRIFTED for d in captured_diffs)
    assert "alerter-poll.timer" in report.skipped_drift_units


# ---------------------------------------------------------------------------
# Busy retry budget (cycles 2.8 + 2.9)
# ---------------------------------------------------------------------------


def test_busy_then_ok_retries_and_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First call returns busy, second returns ok → report records 1 retry."""
    _cfg, config = _write_config(tmp_path)

    busy = ApplyReport(
        written=(),
        skipped_identical=(),
        enabled_timers=(),
        reloaded=False,
        busy=True,
        rejected_reason="concurrent",
    )
    ok = ApplyReport(written=(), skipped_identical=(), enabled_timers=(), reloaded=True)
    _patch_apply(monkeypatch, results=[busy, ok])
    sleeps: list[float] = []

    report = auto_install_scheduled_units(
        config,
        "production",
        fraise_name="alerter",
        policy=AutoInstallPolicy(on_drift="fail"),
        socket_path=tmp_path / "fake.sock",
        is_socket_present=True,
        sleep=sleeps.append,
    )
    assert report.retried_busy == 1
    assert sleeps == [1.0]


def test_busy_exhausts_retry_budget_then_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cfg, config = _write_config(tmp_path)

    busy = ApplyReport(
        written=(),
        skipped_identical=(),
        enabled_timers=(),
        reloaded=False,
        busy=True,
        rejected_reason="concurrent",
    )
    _patch_apply(monkeypatch, results=[busy, busy, busy, busy])
    sleeps: list[float] = []

    with pytest.raises(ScheduledInstallError, match="retry budget exhausted"):
        auto_install_scheduled_units(
            config,
            "production",
            fraise_name="alerter",
            policy=AutoInstallPolicy(on_drift="fail"),
            socket_path=tmp_path / "fake.sock",
            is_socket_present=True,
            sleep=sleeps.append,
        )
    # Phase 0 locked: 3 retries with 1s/3s/10s backoff.
    assert sleeps == [1.0, 3.0, 10.0]


# ---------------------------------------------------------------------------
# Helper rejection surfaces in the operator-facing error
# ---------------------------------------------------------------------------


def test_helper_rejected_response_raises_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cfg, config = _write_config(tmp_path)
    rejected = ApplyReport(
        written=(),
        skipped_identical=(),
        enabled_timers=(),
        reloaded=False,
        rejected_reason="op 0: source outside allowlist",
    )
    _patch_apply(monkeypatch, results=[rejected])

    with pytest.raises(ScheduledInstallError, match="source outside allowlist"):
        auto_install_scheduled_units(
            config,
            "production",
            fraise_name="alerter",
            policy=AutoInstallPolicy(),
            socket_path=tmp_path / "fake.sock",
            is_socket_present=True,
        )

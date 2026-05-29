"""Tests for ``apply_unit_diffs_via_helper`` — #240 Phase 6 client.

The client builds a manifest from a list of ``UnitDiff``s, sends it over a
Unix socket to the unit-installer helper, and translates the helper's
structured response into an ``ApplyReport`` (extended with ``rejected_reason``,
``busy``, ``timed_out`` fields).

Tests fake the helper with a ``socket.socketpair`` and a writer thread.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import TYPE_CHECKING

import pytest

from fraisier.scheduled_install import (
    ApplyReport,
    ScheduledInstallError,
    ScheduledUnitInstall,
    UnitDiff,
    UnitState,
    _apply_via_open_socket,
    _build_apply_report,
    _build_helper_manifest,
    apply_unit_diffs_via_helper,
)

if TYPE_CHECKING:
    from pathlib import Path


def _install(
    *,
    src_dir: Path,
    fraise: str = "alerter",
    env: str = "production",
    job: str = "poll",
    unit: str = "alerter-poll.timer",
    is_timer: bool = True,
    app_path: Path | None = None,
) -> ScheduledUnitInstall:
    app = app_path or src_dir.parent.parent
    return ScheduledUnitInstall(
        fraise_name=fraise,
        environment=env,
        job_name=job,
        unit_name=unit,
        is_timer=is_timer,
        source_path=src_dir / unit,
        dest_path=src_dir.parent.parent.parent / "etc" / "systemd" / "system" / unit,
        app_path=app,
    )


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    src_dir = tmp_path / "app" / "scripts" / "systemd"
    dest_dir = tmp_path / "etc" / "systemd" / "system"
    src_dir.mkdir(parents=True)
    dest_dir.mkdir(parents=True)
    return src_dir, dest_dir


# ---------------------------------------------------------------------------
# Cycle 6.1 — manifest builder
# ---------------------------------------------------------------------------


def test_build_manifest_emits_install_file_op_for_absent_diff(tmp_path: Path) -> None:
    src_dir, _ = _seed(tmp_path)
    src_dir.joinpath("alerter-poll.timer").write_text("[Unit]\n")
    install = _install(src_dir=src_dir)
    diff = UnitDiff(install=install, state=UnitState.ABSENT, diff_summary=None)
    manifest = _build_helper_manifest([diff], force=False, resolved_config_path=None)
    assert len(manifest.operations) == 1
    op = manifest.operations[0]
    assert op.source_path == str(install.source_path)
    assert op.dest_path == str(install.dest_path)
    assert op.mode == "0644"
    assert op.marker is None
    # Timer triggers daemon_reload + enable_now.
    assert len(manifest.post_actions) == 2


def test_build_manifest_includes_marker_when_config_path_given(tmp_path: Path) -> None:
    src_dir, _ = _seed(tmp_path)
    src_dir.joinpath("alerter-poll.timer").write_text("[Unit]\n")
    install = _install(src_dir=src_dir)
    diff = UnitDiff(install=install, state=UnitState.ABSENT, diff_summary=None)
    config_path = tmp_path / "fraises.yaml"
    config_path.write_text("# real config")
    manifest = _build_helper_manifest(
        [diff],
        force=False,
        resolved_config_path=config_path.resolve(strict=True),
    )
    marker = manifest.operations[0].marker
    assert marker is not None
    assert marker.fraises_yaml_path == str(config_path.resolve(strict=True))
    assert marker.fraise_name == "alerter"
    assert marker.environment == "production"
    assert marker.job_name == "poll"


def test_build_manifest_skips_identical_diffs(tmp_path: Path) -> None:
    src_dir, _ = _seed(tmp_path)
    src_dir.joinpath("alerter-poll.timer").write_text("[Unit]\n")
    install = _install(src_dir=src_dir)
    diff = UnitDiff(install=install, state=UnitState.IDENTICAL, diff_summary=None)
    manifest = _build_helper_manifest([diff], force=False, resolved_config_path=None)
    assert manifest.operations == ()
    assert manifest.post_actions == ()


# ---------------------------------------------------------------------------
# #240 follow-up 04 Phase 2 — auto-backfill markers on IDENTICAL diffs
# ---------------------------------------------------------------------------


def test_build_manifest_emits_write_marker_for_identical_with_missing_marker(
    tmp_path: Path,
) -> None:
    """v0.28.0-installed unit (no marker on disk) → auto-backfill via write_marker."""
    from fraisier.unit_installer_protocol import InstallFileOp, WriteMarkerOp

    src_dir, dest_dir = _seed(tmp_path)
    src_dir.joinpath("alerter-poll.timer").write_text("[Unit]\n")
    install = _install(src_dir=src_dir)
    # Pre-existing unit on disk, no marker (the v0.28.0 install scenario).
    dest_dir.joinpath("alerter-poll.timer").write_text("[Unit]\n")
    diff = UnitDiff(install=install, state=UnitState.IDENTICAL, diff_summary=None)
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text("# real")

    manifest = _build_helper_manifest(
        [diff], force=False, resolved_config_path=cfg.resolve(strict=True)
    )
    assert len(manifest.operations) == 1
    op = manifest.operations[0]
    assert isinstance(op, WriteMarkerOp)
    assert not isinstance(op, InstallFileOp)
    assert op.dest_path == str(install.dest_path)
    # No daemon_reload or enable_now — unit already active.
    assert manifest.post_actions == ()


def test_build_manifest_omits_write_marker_when_marker_present(
    tmp_path: Path,
) -> None:
    """Idempotent on re-run: a marker present on disk means no op for that diff."""
    src_dir, dest_dir = _seed(tmp_path)
    src_dir.joinpath("alerter-poll.timer").write_text("[Unit]\n")
    install = _install(src_dir=src_dir)
    dest_dir.joinpath("alerter-poll.timer").write_text("[Unit]\n")
    # Pre-existing marker.
    dest_dir.joinpath("alerter-poll.timer.fraisier-managed").write_text("{}")
    diff = UnitDiff(install=install, state=UnitState.IDENTICAL, diff_summary=None)
    cfg = tmp_path / "fraises.yaml"
    cfg.write_text("# real")

    manifest = _build_helper_manifest(
        [diff], force=False, resolved_config_path=cfg.resolve(strict=True)
    )
    assert manifest.operations == ()


def test_build_manifest_omits_write_marker_without_resolved_config_path(
    tmp_path: Path,
) -> None:
    """resolved_config_path=None means no marker writes regardless of disk state."""
    src_dir, dest_dir = _seed(tmp_path)
    src_dir.joinpath("alerter-poll.timer").write_text("[Unit]\n")
    install = _install(src_dir=src_dir)
    dest_dir.joinpath("alerter-poll.timer").write_text("[Unit]\n")
    diff = UnitDiff(install=install, state=UnitState.IDENTICAL, diff_summary=None)

    manifest = _build_helper_manifest([diff], force=False, resolved_config_path=None)
    assert manifest.operations == ()


# ---------------------------------------------------------------------------
# Cycle 6.2 — response → ApplyReport
# ---------------------------------------------------------------------------


def test_build_apply_report_ok_populates_written_and_enabled(tmp_path: Path) -> None:
    src_dir, _ = _seed(tmp_path)
    install = _install(src_dir=src_dir)
    diff = UnitDiff(install=install, state=UnitState.ABSENT, diff_summary=None)
    response = {
        "status": "ok",
        "installed": ["alerter-poll.timer"],
        "post_actions": [
            {"kind": "daemon-reload", "ok": True},
            {"kind": "enable", "ok": True},
        ],
    }
    report = _build_apply_report(response, [diff])
    assert report.written == (install,)
    assert report.enabled_timers == (install,)
    assert report.reloaded is True
    assert report.rejected_reason is None
    assert report.busy is False
    assert report.timed_out is False


def test_build_apply_report_rejected_carries_reason(tmp_path: Path) -> None:
    src_dir, _ = _seed(tmp_path)
    install = _install(src_dir=src_dir)
    diff = UnitDiff(install=install, state=UnitState.ABSENT, diff_summary=None)
    response = {"status": "rejected", "reason": "source outside allowlist"}
    report = _build_apply_report(response, [diff])
    assert report.written == ()
    assert report.rejected_reason == "source outside allowlist"
    assert report.busy is False
    assert report.timed_out is False


def test_build_apply_report_busy_flag_set(tmp_path: Path) -> None:
    src_dir, _ = _seed(tmp_path)
    install = _install(src_dir=src_dir)
    diff = UnitDiff(install=install, state=UnitState.ABSENT, diff_summary=None)
    response = {"status": "busy", "reason": "concurrent manifest in flight"}
    report = _build_apply_report(response, [diff])
    assert report.busy is True
    assert report.rejected_reason == "concurrent manifest in flight"


def test_build_apply_report_timeout_flag_set(tmp_path: Path) -> None:
    src_dir, _ = _seed(tmp_path)
    install = _install(src_dir=src_dir)
    diff = UnitDiff(install=install, state=UnitState.ABSENT, diff_summary=None)
    response = {"status": "timeout", "reason": "exceeded 300s cap"}
    report = _build_apply_report(response, [diff])
    assert report.timed_out is True
    assert report.rejected_reason == "exceeded 300s cap"


# ---------------------------------------------------------------------------
# End-to-end: apply_unit_diffs_via_helper against a faked socket
# ---------------------------------------------------------------------------


def test_apply_via_open_socket_end_to_end(tmp_path: Path) -> None:
    """Whole _apply_via_open_socket path against a socketpair-backed fake helper."""
    src_dir, _ = _seed(tmp_path)
    src_file = src_dir / "alerter-poll.timer"
    src_file.write_text("[Unit]\n")
    install = _install(src_dir=src_dir)
    diff = UnitDiff(install=install, state=UnitState.ABSENT, diff_summary=None)

    helper_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    def fake_helper() -> None:
        buf = b""
        while b"\n" not in buf:
            chunk = helper_sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        helper_sock.sendall(
            json.dumps(
                {
                    "status": "ok",
                    "installed": ["alerter-poll.timer"],
                    "post_actions": [
                        {"kind": "daemon-reload", "ok": True},
                        {"kind": "enable", "ok": True},
                    ],
                }
            ).encode()
            + b"\n"
        )
        helper_sock.close()

    thread = threading.Thread(target=fake_helper, daemon=True)
    thread.start()
    manifest = _build_helper_manifest([diff], force=False, resolved_config_path=None)
    try:
        report = _apply_via_open_socket(client_sock, manifest, [diff])
    finally:
        client_sock.close()
    thread.join(timeout=5)

    assert report.written == (install,)
    assert report.enabled_timers == (install,)
    assert report.reloaded is True


def _skip_path_safety(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests use tmp_path fixtures so the real dest /etc check would fail."""

    def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr("fraisier.scheduled_install._validate_unit_path_safety", _noop)


def test_apply_unit_diffs_via_helper_raises_on_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_path_safety(monkeypatch)
    src_dir, _ = _seed(tmp_path)
    install = _install(src_dir=src_dir)
    diff = UnitDiff(install=install, state=UnitState.MISSING_SOURCE, diff_summary=None)
    with pytest.raises(ScheduledInstallError, match="source not found"):
        apply_unit_diffs_via_helper([diff], socket_path=tmp_path / "fake.sock")


def test_apply_unit_diffs_via_helper_raises_on_drifted_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_path_safety(monkeypatch)
    src_dir, _ = _seed(tmp_path)
    src_dir.joinpath("alerter-poll.timer").write_text("[Unit]\n")
    install = _install(src_dir=src_dir)
    diff = UnitDiff(install=install, state=UnitState.DRIFTED, diff_summary="hi")
    with pytest.raises(ScheduledInstallError, match="drifted"):
        apply_unit_diffs_via_helper(
            [diff], socket_path=tmp_path / "fake.sock", force=False
        )


def test_apply_unit_diffs_via_helper_requires_config_path_when_marker_write_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_path_safety(monkeypatch)
    src_dir, _ = _seed(tmp_path)
    src_dir.joinpath("alerter-poll.timer").write_text("[Unit]\n")
    install = _install(src_dir=src_dir)
    diff = UnitDiff(install=install, state=UnitState.ABSENT, diff_summary=None)
    with pytest.raises(ScheduledInstallError, match="config_path"):
        apply_unit_diffs_via_helper(
            [diff],
            socket_path=tmp_path / "fake.sock",
            write_markers=True,
            config_path=None,
        )


# ---------------------------------------------------------------------------
# Cycle 6.3 — operator-facing rejection includes the helper's reason
# ---------------------------------------------------------------------------


def test_apply_report_rejected_reason_surfaces_helper_message(tmp_path: Path) -> None:
    """A rejected ApplyReport carries the helper's structured reason verbatim
    so CLIs / deployer logs can surface it to operators."""
    src_dir, _ = _seed(tmp_path)
    install = _install(src_dir=src_dir)
    diff = UnitDiff(install=install, state=UnitState.ABSENT, diff_summary=None)
    report = _build_apply_report(
        {
            "status": "rejected",
            "reason": "op 0: dest parent /tmp/evil is outside every allowlisted dest_prefix",
            "op_index": 0,
        },
        [diff],
    )
    assert isinstance(report, ApplyReport)
    assert "dest parent" in report.rejected_reason
    assert "allowlisted" in report.rejected_reason

"""Tests for fraisier.unit_installer_protocol — pure parse/validate/render.

This module is consumed by the new ``fraisier-unit-installer`` socket helper
(02 Phase 4) and the ``apply_unit_diffs_via_helper`` client (02 Phase 6).
Everything here is pure: no IO, no socket interaction.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from fraisier.unit_installer_protocol import (
    Allowlist,
    AllowlistEntry,
    DaemonReloadAction,
    DisableNowAction,
    EnableNowAction,
    InstallFileOp,
    Manifest,
    ManifestRejected,
    MarkerMeta,
    StopAction,
    WriteMarkerOp,
    parse_manifest,
    render_response,
    serialize_manifest,
    validate_manifest,
)

if TYPE_CHECKING:
    from pathlib import Path


def _canonical_manifest() -> Manifest:
    """Return a known-good manifest fixture: one install_file + daemon_reload."""
    return Manifest(
        version=1,
        deploy_id="2026-05-29T07:14:23Z-abc123",
        operations=(
            InstallFileOp(
                source_path="/var/www/api/scripts/systemd/foo.timer",
                dest_path="/etc/systemd/system/foo.timer",
                mode="0644",
                force=False,
                marker=None,
            ),
        ),
        post_actions=(DaemonReloadAction(),),
    )


def test_parse_then_serialize_round_trips() -> None:
    """``parse_manifest(serialize_manifest(m)) == m`` on a canonical fixture.

    Pins the wire format. Both sides of the socket (helper + client) build the
    same Manifest object graph from the same bytes.
    """
    original = _canonical_manifest()
    wire_bytes = serialize_manifest(original)
    round_tripped = parse_manifest(wire_bytes)
    assert round_tripped == original


# ---------------------------------------------------------------------------
# Phase 1 cycle 1.2 — validate_manifest rejection coverage
# ---------------------------------------------------------------------------


def _seed_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Create allowlisted src/dest dirs under ``tmp_path``.

    Returns ``(src_dir, dest_dir)``. Both exist and are real (not symlinks).
    """
    src_dir = tmp_path / "app" / "scripts" / "systemd"
    dest_dir = tmp_path / "etc" / "systemd" / "system"
    src_dir.mkdir(parents=True)
    dest_dir.mkdir(parents=True)
    return src_dir, dest_dir


def _allowlist(src_dir: Path, dest_dir: Path) -> Allowlist:
    return Allowlist(
        entries=(AllowlistEntry(source_prefix=src_dir, dest_prefix=dest_dir),),
    )


def _good_op(src_dir: Path, dest_dir: Path, unit: str = "foo.timer") -> InstallFileOp:
    """Build an op whose source exists under src_dir and dest sits in dest_dir."""
    src = src_dir / unit
    src.write_text("[Unit]\n")
    return InstallFileOp(
        source_path=str(src),
        dest_path=str(dest_dir / unit),
        mode="0644",
    )


def _manifest(*ops: InstallFileOp, post: tuple[object, ...] = ()) -> Manifest:
    return Manifest(
        version=1,
        deploy_id="test-deploy",
        operations=tuple(ops),
        post_actions=tuple(post),  # ty: ignore[invalid-argument-type]
    )


def test_validate_accepts_minimal_well_formed_manifest(tmp_path: Path) -> None:
    """Baseline: a well-formed manifest passes validation without raising."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    op = _good_op(src_dir, dest_dir)
    validate_manifest(_manifest(op), _allowlist(src_dir, dest_dir))


def test_validate_rejects_unauthorized_source_prefix(tmp_path: Path) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    bad = other / "foo.timer"
    bad.write_text("x")
    op = InstallFileOp(
        source_path=str(bad),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
    )
    with pytest.raises(ManifestRejected, match="source"):
        validate_manifest(_manifest(op), _allowlist(src_dir, dest_dir))


def test_validate_rejects_source_symlink_escape(tmp_path: Path) -> None:
    """Symlink inside the source tree pointing outside is rejected after resolve()."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    real_target = outside / "evil.timer"
    real_target.write_text("evil")
    sneaky = src_dir / "foo.timer"
    sneaky.symlink_to(real_target)
    op = InstallFileOp(
        source_path=str(sneaky),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
    )
    with pytest.raises(ManifestRejected, match="source"):
        validate_manifest(_manifest(op), _allowlist(src_dir, dest_dir))


def test_validate_rejects_dest_parent_outside_allowlist(tmp_path: Path) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    bad_dest_dir = tmp_path / "etc" / "cron.d"
    bad_dest_dir.mkdir(parents=True)
    op = InstallFileOp(
        source_path=str(_good_op(src_dir, dest_dir).source_path),
        dest_path=str(bad_dest_dir / "foo.timer"),
        mode="0644",
    )
    with pytest.raises(ManifestRejected, match="dest"):
        validate_manifest(_manifest(op), _allowlist(src_dir, dest_dir))


def test_validate_rejects_mode_other_than_0644(tmp_path: Path) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    base = _good_op(src_dir, dest_dir)
    op = InstallFileOp(
        source_path=base.source_path,
        dest_path=base.dest_path,
        mode="0755",
    )
    with pytest.raises(ManifestRejected, match="mode"):
        validate_manifest(_manifest(op), _allowlist(src_dir, dest_dir))


def test_validate_rejects_extra_path_component_in_dest(tmp_path: Path) -> None:
    """An extra path component in dest_path makes the parent mismatch dest_prefix."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    (dest_dir / "sub").mkdir()
    op = InstallFileOp(
        source_path=str(src_dir / "foo.timer"),
        dest_path=str(dest_dir / "sub" / "foo.timer"),
        mode="0644",
    )
    (src_dir / "foo.timer").write_text("x")
    with pytest.raises(ManifestRejected, match="dest"):
        validate_manifest(_manifest(op), _allowlist(src_dir, dest_dir))


def test_validate_rejects_dotdot_in_basename(tmp_path: Path) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    op = InstallFileOp(
        source_path=str(src_dir / "foo.timer"),
        dest_path=str(dest_dir / "..foo.timer"),
        mode="0644",
    )
    (src_dir / "foo.timer").write_text("x")
    with pytest.raises(ManifestRejected, match="basename"):
        validate_manifest(_manifest(op), _allowlist(src_dir, dest_dir))


def test_validate_rejects_leading_dot_basename(tmp_path: Path) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    op = InstallFileOp(
        source_path=str(src_dir / "foo.timer"),
        dest_path=str(dest_dir / ".foo.timer"),
        mode="0644",
    )
    (src_dir / "foo.timer").write_text("x")
    with pytest.raises(ManifestRejected, match="basename"):
        validate_manifest(_manifest(op), _allowlist(src_dir, dest_dir))


def test_validate_rejects_basename_failing_validate_service_name(
    tmp_path: Path,
) -> None:
    """A name with a shell metacharacter fails ``validate_service_name``."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    op = InstallFileOp(
        source_path=str(src_dir / "foo.timer"),
        dest_path=str(dest_dir / "foo;rm.timer"),
        mode="0644",
    )
    (src_dir / "foo.timer").write_text("x")
    with pytest.raises(ManifestRejected, match="basename"):
        validate_manifest(_manifest(op), _allowlist(src_dir, dest_dir))


def test_validate_rejects_enable_now_not_in_install_files(tmp_path: Path) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    op = _good_op(src_dir, dest_dir, unit="foo.timer")
    enable = EnableNowAction(unit="bar.timer")  # not installed by this manifest
    with pytest.raises(ManifestRejected, match="enable_now"):
        validate_manifest(_manifest(op, post=(enable,)), _allowlist(src_dir, dest_dir))


def test_validate_accepts_enable_now_for_unit_in_install_files(tmp_path: Path) -> None:
    """The same constraint allows enable_now when the unit IS being installed."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    op = _good_op(src_dir, dest_dir, unit="foo.timer")
    enable = EnableNowAction(unit="foo.timer")
    validate_manifest(_manifest(op, post=(enable,)), _allowlist(src_dir, dest_dir))


def test_validate_rejects_unknown_version(tmp_path: Path) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    op = _good_op(src_dir, dest_dir)
    bad = Manifest(
        version=999,
        deploy_id="test",
        operations=(op,),
        post_actions=(),
    )
    with pytest.raises(ManifestRejected, match="version"):
        validate_manifest(bad, _allowlist(src_dir, dest_dir))


def test_parse_rejects_manifest_over_one_mib() -> None:
    """A wire payload >1 MiB is rejected before JSON decode."""
    payload = b"x" * (1024 * 1024 + 1)
    with pytest.raises(ManifestRejected, match="too large"):
        parse_manifest(payload)


def test_parse_rejects_missing_version_field() -> None:
    raw = (
        json.dumps({"deploy_id": "x", "operations": [], "post_actions": []}).encode()
        + b"\n"
    )
    with pytest.raises(ManifestRejected, match="version"):
        parse_manifest(raw)


# ---------------------------------------------------------------------------
# Phase 1 cycle 1.3 — structured rejection responses
# ---------------------------------------------------------------------------


def test_render_response_rejected_carries_reason_and_op_index() -> None:
    """``render_response("rejected", ...)`` shapes the wire response strictly."""
    wire = render_response("rejected", reason="bad mode", op_index=2)
    assert wire.endswith(b"\n"), "responses must terminate with a newline"
    decoded = json.loads(wire)
    assert decoded == {"status": "rejected", "reason": "bad mode", "op_index": 2}


def test_render_response_ok_drops_optional_fields() -> None:
    """Optional kwargs are omitted when not provided (no ``op_index: null`` noise)."""
    wire = render_response("ok")
    decoded = json.loads(wire)
    assert decoded == {"status": "ok"}


def test_render_response_busy_with_reason() -> None:
    """``busy`` carries the flock collision reason; no op_index."""
    wire = render_response("busy", reason="concurrent manifest in flight")
    decoded = json.loads(wire)
    assert decoded == {
        "status": "busy",
        "reason": "concurrent manifest in flight",
    }


# ---------------------------------------------------------------------------
# Phase 1 cycle 1.4 — marker validation
# ---------------------------------------------------------------------------


def _absolute_marker() -> MarkerMeta:
    return MarkerMeta(
        fraises_yaml_path="/opt/myproj/fraises.yaml",
        fraise_name="alerter",
        environment="production",
        job_name="poll",
    )


def test_validate_rejects_relative_marker_yaml_path(tmp_path: Path) -> None:
    """Caller is responsible for resolving; validator enforces the invariant."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    base = _good_op(src_dir, dest_dir)
    marker = MarkerMeta(
        fraises_yaml_path="relative/fraises.yaml",
        fraise_name="alerter",
        environment="production",
        job_name="poll",
    )
    op = InstallFileOp(
        source_path=base.source_path,
        dest_path=base.dest_path,
        mode="0644",
        marker=marker,
    )
    with pytest.raises(ManifestRejected, match="marker"):
        validate_manifest(_manifest(op), _allowlist(src_dir, dest_dir))


def test_validate_accepts_op_with_absolute_marker_yaml_path(tmp_path: Path) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    base = _good_op(src_dir, dest_dir)
    op = InstallFileOp(
        source_path=base.source_path,
        dest_path=base.dest_path,
        mode="0644",
        marker=_absolute_marker(),
    )
    validate_manifest(_manifest(op), _allowlist(src_dir, dest_dir))


def test_marker_round_trips_through_wire_format() -> None:
    """Marker survives serialize → parse on a canonical install_file op."""
    op = InstallFileOp(
        source_path="/var/www/api/scripts/systemd/foo.timer",
        dest_path="/etc/systemd/system/foo.timer",
        mode="0644",
        marker=_absolute_marker(),
    )
    original = Manifest(version=1, deploy_id="t", operations=(op,))
    decoded = parse_manifest(serialize_manifest(original))
    assert decoded == original
    assert decoded.operations[0].marker == _absolute_marker()


# ---------------------------------------------------------------------------
# Phase 1 cycle 1.5 — validator accepts a full well-formed manifest
# ---------------------------------------------------------------------------


def test_validate_accepts_full_manifest_with_all_post_action_kinds(
    tmp_path: Path,
) -> None:
    """Install + marker + every post-action kind passes validation.

    disable_now/stop intentionally skip the install_file basename constraint
    (they're used by 04's prune path against units already on disk); helper
    runtime layer (Phase 4) enforces marker-presence for those.
    """
    src_dir, dest_dir = _seed_layout(tmp_path)
    install = InstallFileOp(
        source_path=str(src_dir / "foo.timer"),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
        marker=_absolute_marker(),
    )
    (src_dir / "foo.timer").write_text("[Unit]\n")
    manifest = Manifest(
        version=1,
        deploy_id="full",
        operations=(install,),
        post_actions=(
            DaemonReloadAction(),
            EnableNowAction(unit="foo.timer"),
            DisableNowAction(unit="orphan.timer"),
            StopAction(unit="orphan.service"),
        ),
    )
    validate_manifest(manifest, _allowlist(src_dir, dest_dir))


# ---------------------------------------------------------------------------
# #240 follow-up 04 Phase 2 — write_marker op kind
# ---------------------------------------------------------------------------


def test_write_marker_op_round_trips_through_wire_format() -> None:
    op = WriteMarkerOp(
        dest_path="/etc/systemd/system/foo.timer",
        marker=_absolute_marker(),
    )
    original = Manifest(version=1, deploy_id="t", operations=(op,))
    decoded = parse_manifest(serialize_manifest(original))
    assert decoded == original
    assert isinstance(decoded.operations[0], WriteMarkerOp)


def test_validate_accepts_write_marker_op_with_allowlisted_dest(
    tmp_path: Path,
) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    op = WriteMarkerOp(
        dest_path=str(dest_dir / "foo.timer"),
        marker=_absolute_marker(),
    )
    manifest = Manifest(version=1, deploy_id="t", operations=(op,))
    validate_manifest(manifest, _allowlist(src_dir, dest_dir))


def test_validate_rejects_write_marker_dest_outside_allowlist(
    tmp_path: Path,
) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    op = WriteMarkerOp(
        dest_path=str(elsewhere / "foo.timer"),
        marker=_absolute_marker(),
    )
    manifest = Manifest(version=1, deploy_id="t", operations=(op,))
    with pytest.raises(ManifestRejected, match="dest"):
        validate_manifest(manifest, _allowlist(src_dir, dest_dir))


def test_validate_rejects_write_marker_relative_marker_yaml_path(
    tmp_path: Path,
) -> None:
    src_dir, dest_dir = _seed_layout(tmp_path)
    bad_marker = MarkerMeta(
        fraises_yaml_path="relative/fraises.yaml",
        fraise_name="alerter",
        environment="production",
        job_name="poll",
    )
    op = WriteMarkerOp(dest_path=str(dest_dir / "foo.timer"), marker=bad_marker)
    manifest = Manifest(version=1, deploy_id="t", operations=(op,))
    with pytest.raises(ManifestRejected, match="marker"):
        validate_manifest(manifest, _allowlist(src_dir, dest_dir))


def test_write_marker_does_not_contribute_to_enable_now_basenames(
    tmp_path: Path,
) -> None:
    """A write_marker op for foo.timer does NOT authorise an
    enable_now foo.timer in the same manifest. The unit isn't being
    installed here; if the caller wants to enable, they should send an
    install_file op."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    wm = WriteMarkerOp(dest_path=str(dest_dir / "foo.timer"), marker=_absolute_marker())
    enable = EnableNowAction(unit="foo.timer")
    manifest = Manifest(
        version=1, deploy_id="t", operations=(wm,), post_actions=(enable,)
    )
    with pytest.raises(ManifestRejected, match="enable_now"):
        validate_manifest(manifest, _allowlist(src_dir, dest_dir))


def test_full_manifest_round_trips_through_wire_format(tmp_path: Path) -> None:
    """All op kinds survive serialize → parse."""
    src_dir, dest_dir = _seed_layout(tmp_path)
    install = InstallFileOp(
        source_path=str(src_dir / "foo.timer"),
        dest_path=str(dest_dir / "foo.timer"),
        mode="0644",
        marker=_absolute_marker(),
    )
    original = Manifest(
        version=1,
        deploy_id="full",
        operations=(install,),
        post_actions=(
            DaemonReloadAction(),
            EnableNowAction(unit="foo.timer"),
            DisableNowAction(unit="orphan.timer"),
            StopAction(unit="orphan.service"),
        ),
    )
    decoded = parse_manifest(serialize_manifest(original))
    assert decoded == original

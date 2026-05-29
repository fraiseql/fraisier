"""Tests for fraisier.unit_installer_protocol — pure parse/validate/render.

This module is consumed by the new ``fraisier-unit-installer`` socket helper
(02 Phase 4) and the ``apply_unit_diffs_via_helper`` client (02 Phase 6).
Everything here is pure: no IO, no socket interaction.
"""

from __future__ import annotations

from fraisier.unit_installer_protocol import (
    DaemonReloadAction,
    InstallFileOp,
    Manifest,
    parse_manifest,
    serialize_manifest,
)


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

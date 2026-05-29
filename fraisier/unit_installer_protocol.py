"""Wire protocol for the ``fraisier-unit-installer`` socket helper.

Pure parse + serialize + validate. No IO. Imported by both sides of the
socket — the helper daemon (02 Phase 4) and the
``apply_unit_diffs_via_helper`` client (02 Phase 6).

Wire format: one JSON object terminated by ``\\n`` carrying ``version``,
``deploy_id``, ``operations``, and ``post_actions``. Envelope ships
``version: 1`` from day one (locked Phase 0 decision).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MANIFEST_VERSION = 1


@dataclass(frozen=True)
class InstallFileOp:
    """File-install operation: copy source bytes to dest, chmod ``mode``."""

    source_path: str
    dest_path: str
    mode: str
    force: bool = False
    marker: None = None  # MarkerMeta lands in cycle 1.4


@dataclass(frozen=True)
class DaemonReloadAction:
    """``systemctl daemon-reload`` post-action."""


@dataclass(frozen=True)
class Manifest:
    """One end-to-end install request."""

    version: int
    deploy_id: str
    operations: tuple[InstallFileOp, ...] = ()
    post_actions: tuple[DaemonReloadAction, ...] = ()


def serialize_manifest(manifest: Manifest) -> bytes:
    """Encode ``manifest`` as one JSON line terminated by ``\\n``."""
    payload: dict[str, Any] = {
        "version": manifest.version,
        "deploy_id": manifest.deploy_id,
        "operations": [_op_to_json(op) for op in manifest.operations],
        "post_actions": [_action_to_json(a) for a in manifest.post_actions],
    }
    return json.dumps(payload).encode() + b"\n"


def parse_manifest(raw: bytes) -> Manifest:
    """Decode wire bytes into a ``Manifest``."""
    payload = json.loads(raw)
    return Manifest(
        version=payload["version"],
        deploy_id=payload["deploy_id"],
        operations=tuple(_op_from_json(op) for op in payload.get("operations", [])),
        post_actions=tuple(
            _action_from_json(a) for a in payload.get("post_actions", [])
        ),
    )


def _op_to_json(op: InstallFileOp) -> dict[str, Any]:
    return {
        "kind": "install_file",
        "source_path": op.source_path,
        "dest_path": op.dest_path,
        "mode": op.mode,
        "force": op.force,
        "marker": op.marker,
    }


def _op_from_json(data: dict[str, Any]) -> InstallFileOp:
    return InstallFileOp(
        source_path=data["source_path"],
        dest_path=data["dest_path"],
        mode=data["mode"],
        force=data.get("force", False),
        marker=data.get("marker"),
    )


def _action_to_json(action: DaemonReloadAction) -> dict[str, Any]:
    match action:
        case DaemonReloadAction():
            return {"kind": "daemon_reload"}
    msg = f"unsupported post-action: {action!r}"
    raise TypeError(msg)


def _action_from_json(data: dict[str, Any]) -> DaemonReloadAction:
    kind = data["kind"]
    if kind == "daemon_reload":
        return DaemonReloadAction()
    msg = f"unknown post-action kind: {kind!r}"
    raise ValueError(msg)

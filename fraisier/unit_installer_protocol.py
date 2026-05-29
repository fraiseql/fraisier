"""Wire protocol for the ``fraisier-unit-installer`` socket helper.

Pure parse + serialize + validate. No mutating IO. Imported by both sides of
the socket — the helper daemon (02 Phase 4) and the
``apply_unit_diffs_via_helper`` client (02 Phase 6).

Wire format: one JSON object terminated by ``\\n`` carrying ``version``,
``deploy_id``, ``operations``, and ``post_actions``. Envelope ships
``version: 1`` from day one (locked Phase 0 decision).

Note on ``Path.resolve``: ``validate_manifest`` calls ``Path.resolve(strict=True)``
on source/dest-parent paths to defeat symlink-escape. That reads symlink
targets but performs no mutating IO; the function is referentially transparent
given a fixed filesystem snapshot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fraisier.dbops._validation import validate_service_name

MANIFEST_VERSION = 1
"""Wire version shipped by every manifest in v0.29.0."""

MAX_MANIFEST_BYTES = 1024 * 1024
"""1 MiB hard cap on incoming wire payloads (defends against malicious oversize)."""

REQUIRED_MODE = "0644"
"""The only file mode the helper will install. No exec, no setuid."""


class ManifestRejected(Exception):
    """Raised by ``parse_manifest`` / ``validate_manifest`` on malformed input.

    Carries a free-text reason. Callers translate to a structured rejection
    response via ``render_response("rejected", reason=..., op_index=...)``.
    """


# ---------------------------------------------------------------------------
# Operations (the body of a manifest)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallFileOp:
    """File-install operation: copy source bytes to dest, chmod ``mode``."""

    source_path: str
    dest_path: str
    mode: str
    force: bool = False
    marker: None = None  # MarkerMeta lands in cycle 1.4


# ---------------------------------------------------------------------------
# Post-actions (systemctl invocations sequenced after ops)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DaemonReloadAction:
    """``systemctl daemon-reload`` post-action."""


@dataclass(frozen=True)
class EnableNowAction:
    """``systemctl enable --now <unit>`` post-action."""

    unit: str


PostAction = DaemonReloadAction | EnableNowAction


# ---------------------------------------------------------------------------
# Manifest envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Manifest:
    """One end-to-end install request."""

    version: int
    deploy_id: str
    operations: tuple[InstallFileOp, ...] = ()
    post_actions: tuple[PostAction, ...] = ()


# ---------------------------------------------------------------------------
# Allowlist (the security boundary baked in at render time)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AllowlistEntry:
    """One (source_prefix, dest_prefix) pair the helper accepts."""

    source_prefix: Path
    dest_prefix: Path


@dataclass(frozen=True)
class Allowlist:
    """The full set of accepted (source_prefix, dest_prefix) pairs."""

    entries: tuple[AllowlistEntry, ...]

    def match_source_prefix(self, resolved_source: Path) -> AllowlistEntry | None:
        """Return the entry whose ``source_prefix`` covers ``resolved_source``."""
        for entry in self.entries:
            try:
                src_root = entry.source_prefix.resolve(strict=True)
            except FileNotFoundError:
                continue
            if resolved_source.is_relative_to(src_root):
                return entry
        return None


# ---------------------------------------------------------------------------
# parse / serialize
# ---------------------------------------------------------------------------


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
    """Decode wire bytes into a ``Manifest``.

    Raises ``ManifestRejected`` on oversize, malformed JSON, or missing fields.
    """
    if len(raw) > MAX_MANIFEST_BYTES:
        msg = f"manifest too large: {len(raw)} bytes (cap {MAX_MANIFEST_BYTES})"
        raise ManifestRejected(msg)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"malformed JSON: {exc}"
        raise ManifestRejected(msg) from exc
    if not isinstance(payload, dict):
        msg = "manifest must be a JSON object"
        raise ManifestRejected(msg)
    if "version" not in payload:
        msg = "missing required field: version"
        raise ManifestRejected(msg)
    if "deploy_id" not in payload:
        msg = "missing required field: deploy_id"
        raise ManifestRejected(msg)
    return Manifest(
        version=payload["version"],
        deploy_id=payload["deploy_id"],
        operations=tuple(_op_from_json(op) for op in payload.get("operations", [])),
        post_actions=tuple(
            _action_from_json(a) for a in payload.get("post_actions", [])
        ),
    )


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def validate_manifest(manifest: Manifest, allowlist: Allowlist) -> None:
    """Raise ``ManifestRejected`` if ``manifest`` violates any invariant.

    Order of checks (predictable failure mode for tests + ops):

    1. Envelope: ``version`` is supported.
    2. For each ``InstallFileOp``: mode → basename → source allowlist → dest parent.
    3. For each post-action: ``EnableNowAction.unit`` must appear as an
       install_file dest basename in the same manifest.
    """
    if manifest.version != MANIFEST_VERSION:
        msg = f"unsupported manifest version: {manifest.version}"
        raise ManifestRejected(msg)

    installed_basenames: set[str] = set()
    for index, op in enumerate(manifest.operations):
        _validate_install_file_op(op, allowlist, op_index=index)
        installed_basenames.add(Path(op.dest_path).name)

    for index, action in enumerate(manifest.post_actions):
        _validate_post_action(action, installed_basenames, action_index=index)


def _validate_install_file_op(
    op: InstallFileOp,
    allowlist: Allowlist,
    *,
    op_index: int,
) -> None:
    if op.mode != REQUIRED_MODE:
        msg = (
            f"op {op_index}: mode {op.mode!r} not permitted (must be {REQUIRED_MODE!r})"
        )
        raise ManifestRejected(msg)

    basename = Path(op.dest_path).name
    _check_unit_basename(basename, op_index=op_index)

    _check_source(op.source_path, allowlist, op_index=op_index)
    _check_dest_parent(op.dest_path, allowlist, op_index=op_index)


def _check_unit_basename(basename: str, *, op_index: int) -> None:
    if ".." in basename:
        msg = f"op {op_index}: basename {basename!r} contains '..'"
        raise ManifestRejected(msg)
    if basename.startswith("."):
        msg = f"op {op_index}: basename {basename!r} has a leading dot"
        raise ManifestRejected(msg)
    try:
        validate_service_name(basename)
    except ValueError as exc:
        msg = f"op {op_index}: basename {basename!r} invalid: {exc}"
        raise ManifestRejected(msg) from exc


def _check_source(source_path: str, allowlist: Allowlist, *, op_index: int) -> None:
    try:
        resolved = Path(source_path).resolve(strict=True)
    except FileNotFoundError as exc:
        msg = f"op {op_index}: source path does not exist: {source_path}"
        raise ManifestRejected(msg) from exc
    if allowlist.match_source_prefix(resolved) is None:
        msg = (
            f"op {op_index}: source {source_path} (resolves to {resolved}) "
            "is outside every allowlisted source_prefix"
        )
        raise ManifestRejected(msg)


def _check_dest_parent(dest_path: str, allowlist: Allowlist, *, op_index: int) -> None:
    try:
        actual_parent = Path(dest_path).parent.resolve(strict=True)
    except FileNotFoundError as exc:
        msg = f"op {op_index}: dest parent does not exist: {Path(dest_path).parent}"
        raise ManifestRejected(msg) from exc
    for entry in allowlist.entries:
        try:
            expected = entry.dest_prefix.resolve(strict=True)
        except FileNotFoundError:
            continue
        if actual_parent == expected:
            return
    msg = (
        f"op {op_index}: dest parent {actual_parent} is outside every "
        "allowlisted dest_prefix"
    )
    raise ManifestRejected(msg)


def _validate_post_action(
    action: PostAction,
    installed_basenames: set[str],
    *,
    action_index: int,
) -> None:
    match action:
        case DaemonReloadAction():
            return
        case EnableNowAction(unit=unit):
            if unit not in installed_basenames:
                msg = (
                    f"post_action {action_index}: enable_now {unit!r} refers to "
                    "a unit not installed by this manifest"
                )
                raise ManifestRejected(msg)


# ---------------------------------------------------------------------------
# JSON encoding helpers
# ---------------------------------------------------------------------------


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


def _action_to_json(action: PostAction) -> dict[str, Any]:
    match action:
        case DaemonReloadAction():
            return {"kind": "daemon_reload"}
        case EnableNowAction(unit=unit):
            return {"kind": "enable_now", "unit": unit}
    msg = f"unsupported post-action: {action!r}"
    raise TypeError(msg)


def _action_from_json(data: dict[str, Any]) -> PostAction:
    kind = data["kind"]
    if kind == "daemon_reload":
        return DaemonReloadAction()
    if kind == "enable_now":
        return EnableNowAction(unit=data["unit"])
    msg = f"unknown post-action kind: {kind!r}"
    raise ValueError(msg)

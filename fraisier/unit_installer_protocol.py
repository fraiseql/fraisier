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
class MarkerMeta:
    """Sidecar marker payload — advisory, not authenticated.

    Phase 04 of bundle A consumes these via ``prune_orphans`` to identify
    fraisier-managed units. ``fraises_yaml_path`` MUST be absolute (typically
    the caller's ``Path.resolve(strict=True)`` output) so prune planners
    started from different working directories converge on the same identity.
    """

    fraises_yaml_path: str
    fraise_name: str
    environment: str
    job_name: str


@dataclass(frozen=True)
class InstallFileOp:
    """File-install operation: copy source bytes to dest, chmod ``mode``."""

    source_path: str
    dest_path: str
    mode: str
    force: bool = False
    marker: MarkerMeta | None = None


@dataclass(frozen=True)
class WriteMarkerOp:
    """Sidecar-only op (#240 follow-up 04): write the marker, don't touch the unit.

    Used by ``apply_unit_diffs_via_helper`` to backfill markers on v0.28.0
    hosts: on an ``IDENTICAL`` diff whose ``.fraisier-managed`` sidecar is
    missing, the client emits this op instead of ``InstallFileOp``. Avoids
    re-reading the existing unit file just to confirm-by-rewrite.

    ``dest_path`` is the full path to the *unit* (e.g.,
    ``/etc/systemd/system/foo.timer``); the helper writes the marker at
    ``<dest_path>.fraisier-managed``. Same allowlist + O_NOFOLLOW discipline
    applies as ``InstallFileOp``.
    """

    dest_path: str
    marker: MarkerMeta


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


@dataclass(frozen=True)
class DisableNowAction:
    """``systemctl disable --now <unit>`` post-action (prune path).

    Validator does NOT constrain ``unit`` against the same manifest's
    install_file basenames — the helper's runtime layer (Phase 4) checks
    marker-presence on disk before executing.
    """

    unit: str


@dataclass(frozen=True)
class StopAction:
    """``systemctl stop <unit>`` post-action (prune path).

    Same runtime-only constraint as ``DisableNowAction``.
    """

    unit: str


PostAction = DaemonReloadAction | EnableNowAction | DisableNowAction | StopAction


# ---------------------------------------------------------------------------
# Manifest envelope
# ---------------------------------------------------------------------------


Operation = InstallFileOp | WriteMarkerOp


@dataclass(frozen=True)
class Manifest:
    """One end-to-end install request."""

    version: int
    deploy_id: str
    operations: tuple[Operation, ...] = ()
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
            src_root = _safe_resolve(entry.source_prefix)
            if src_root is not None and resolved_source.is_relative_to(src_root):
                return entry
        return None

    def match_dest_prefix(self, resolved_dest_parent: Path) -> AllowlistEntry | None:
        """Return the entry whose ``dest_prefix`` equals ``resolved_dest_parent``."""
        for entry in self.entries:
            dest_root = _safe_resolve(entry.dest_prefix)
            if dest_root is not None and resolved_dest_parent == dest_root:
                return entry
        return None


def _safe_resolve(path: Path) -> Path | None:
    """``Path.resolve(strict=True)`` returning ``None`` for missing entries."""
    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Response wire format (helper → client)
# ---------------------------------------------------------------------------


def render_response(
    status: str,
    *,
    reason: str | None = None,
    op_index: int | None = None,
    **extra: Any,
) -> bytes:
    """Encode a helper response as one JSON line terminated by ``\\n``.

    ``status`` is one of ``"ok"``, ``"rejected"``, ``"busy"``, ``"timeout"``.
    Optional kwargs are dropped when ``None`` (no ``op_index: null`` noise on
    the wire). Callers pass any additional structured fields through ``extra``.
    """
    payload: dict[str, Any] = {"status": status}
    if reason is not None:
        payload["reason"] = reason
    if op_index is not None:
        payload["op_index"] = op_index
    payload.update(extra)
    return json.dumps(payload).encode() + b"\n"


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
        match op:
            case InstallFileOp():
                _validate_install_file_op(op, allowlist, op_index=index)
                installed_basenames.add(Path(op.dest_path).name)
            case WriteMarkerOp():
                _validate_write_marker_op(op, allowlist, op_index=index)
                # Marker-only ops do NOT contribute to installed_basenames —
                # they don't install a unit; an enable_now in the same
                # manifest can't legitimately target them.

    for index, action in enumerate(manifest.post_actions):
        _validate_post_action(action, installed_basenames, action_index=index)


def _validate_write_marker_op(
    op: WriteMarkerOp,
    allowlist: Allowlist,
    *,
    op_index: int,
) -> None:
    basename = Path(op.dest_path).name
    _check_unit_basename(basename, op_index=op_index)
    _check_dest_parent(op.dest_path, allowlist, op_index=op_index)
    _check_marker(op.marker, op_index=op_index)


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

    if op.marker is not None:
        _check_marker(op.marker, op_index=op_index)


def _check_marker(marker: MarkerMeta, *, op_index: int) -> None:
    if not Path(marker.fraises_yaml_path).is_absolute():
        msg = (
            f"op {op_index}: marker fraises_yaml_path "
            f"{marker.fraises_yaml_path!r} is not absolute "
            "(caller must Path.resolve() before sending)"
        )
        raise ManifestRejected(msg)


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
    if allowlist.match_dest_prefix(actual_parent) is None:
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
        case DaemonReloadAction() | DisableNowAction() | StopAction():
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


def _op_to_json(op: Operation) -> dict[str, Any]:
    match op:
        case InstallFileOp():
            return {
                "kind": "install_file",
                "source_path": op.source_path,
                "dest_path": op.dest_path,
                "mode": op.mode,
                "force": op.force,
                "marker": _marker_to_json(op.marker),
            }
        case WriteMarkerOp():
            return {
                "kind": "write_marker",
                "dest_path": op.dest_path,
                "marker": _marker_to_json(op.marker),
            }
    msg = f"unsupported op: {op!r}"
    raise TypeError(msg)


def _op_from_json(data: dict[str, Any]) -> Operation:
    kind = data.get("kind", "install_file")
    if kind == "install_file":
        return InstallFileOp(
            source_path=data["source_path"],
            dest_path=data["dest_path"],
            mode=data["mode"],
            force=data.get("force", False),
            marker=_marker_from_json(data.get("marker")),
        )
    if kind == "write_marker":
        marker = _marker_from_json(data.get("marker"))
        if marker is None:
            msg = "write_marker op requires a marker payload"
            raise ManifestRejected(msg)
        return WriteMarkerOp(dest_path=data["dest_path"], marker=marker)
    msg = f"unknown op kind: {kind!r}"
    raise ManifestRejected(msg)


def _marker_to_json(marker: MarkerMeta | None) -> dict[str, Any] | None:
    if marker is None:
        return None
    return {
        "fraises_yaml_path": marker.fraises_yaml_path,
        "fraise_name": marker.fraise_name,
        "environment": marker.environment,
        "job_name": marker.job_name,
    }


def _marker_from_json(data: dict[str, Any] | None) -> MarkerMeta | None:
    if data is None:
        return None
    return MarkerMeta(
        fraises_yaml_path=data["fraises_yaml_path"],
        fraise_name=data["fraise_name"],
        environment=data["environment"],
        job_name=data["job_name"],
    )


def _action_to_json(action: PostAction) -> dict[str, Any]:
    match action:
        case DaemonReloadAction():
            return {"kind": "daemon_reload"}
        case EnableNowAction(unit=unit):
            return {"kind": "enable_now", "unit": unit}
        case DisableNowAction(unit=unit):
            return {"kind": "disable_now", "unit": unit}
        case StopAction(unit=unit):
            return {"kind": "stop", "unit": unit}
    msg = f"unsupported post-action: {action!r}"
    raise TypeError(msg)


def _action_from_json(data: dict[str, Any]) -> PostAction:
    kind = data["kind"]
    if kind == "daemon_reload":
        return DaemonReloadAction()
    if kind == "enable_now":
        return EnableNowAction(unit=data["unit"])
    if kind == "disable_now":
        return DisableNowAction(unit=data["unit"])
    if kind == "stop":
        return StopAction(unit=data["unit"])
    msg = f"unknown post-action kind: {kind!r}"
    raise ValueError(msg)

"""Pure layers behind ``fraisier scheduled-install``.

This module is consumed by ``fraisier/cli/scheduled_install.py``. It owns:

- ``enumerate_scheduled_units``: walks an already-loaded ``FraisierConfig`` and
  yields one ``ScheduledUnitInstall`` per ``systemd_service`` / ``systemd_timer``
  declared on ``type: scheduled`` fraises' ``jobs.*``.

The source-path convention is ``<env.app_path>/scripts/systemd/<unit_name>`` —
the consumer's hand-authored unit files, NOT ``scripts/generated/systemd/``
(which is fraisier's own scaffold output for webhook / install-helper units).
"""

from __future__ import annotations

import difflib
import shutil
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from fraisier.dbops._validation import validate_service_name

if TYPE_CHECKING:
    from typing import Any

    from fraisier.config import FraisierConfig
    from fraisier.runners import CommandRunner
    from fraisier.unit_installer_protocol import Manifest, MarkerMeta

SYSTEMD_DEST_DIR = Path("/etc/systemd/system")
APP_PATH_UNITS_SUBDIR = Path("scripts/systemd")

# Marker convention (#240 follow-up 04). Sidecar file next to each fraisier-
# managed systemd unit. Advisory, not authenticated — see the MarkerMeta
# docstring in fraisier.unit_installer_protocol for the threat model.
MARKER_SUFFIX = ".fraisier-managed"


class ScheduledInstallError(Exception):
    """Raised when ``apply_unit_diffs`` refuses to converge.

    Covers MISSING_SOURCE, DRIFTED-without-``force``, and path-traversal
    violations. Always raised *before* any filesystem mutation.
    """


@dataclass(frozen=True)
class ScheduledUnitInstall:
    """One systemd unit (service or timer) declared by a ``type: scheduled`` job."""

    fraise_name: str
    environment: str
    job_name: str
    unit_name: str
    is_timer: bool
    source_path: Path
    dest_path: Path
    app_path: Path  # consumer's app_path; Phase 03 uses it for source-containment check


class UnitState(StrEnum):
    """Per-unit reconciliation state determined by ``classify_unit``."""

    ABSENT = "absent"  # dest does not exist; source does
    IDENTICAL = "identical"  # dest exists, byte-equal to source
    DRIFTED = "drifted"  # dest exists, differs from source
    MISSING_SOURCE = "missing"  # source does not exist (operator error)


@dataclass(frozen=True)
class UnitDiff:
    """Result of classifying one ``ScheduledUnitInstall`` against the filesystem."""

    install: ScheduledUnitInstall
    state: UnitState
    diff_summary: str | None  # short one-line summary for DRIFTED; None otherwise


def enumerate_scheduled_units(
    config: FraisierConfig, environment: str
) -> list[ScheduledUnitInstall]:
    """Return the unit-install rows for ``environment`` across all scheduled fraises."""
    units: list[ScheduledUnitInstall] = []
    for fraise_name, fraise in config.fraises.items():
        if fraise.get("type") != "scheduled":
            continue
        env_config = (fraise.get("environments") or {}).get(environment)
        if env_config is None:
            continue
        app_path = Path(env_config["app_path"])
        for job_name, job in (env_config.get("jobs") or {}).items():
            for field, is_timer in (
                ("systemd_service", False),
                ("systemd_timer", True),
            ):
                unit_name = job.get(field)
                if not unit_name:
                    continue
                validate_service_name(unit_name)
                units.append(
                    ScheduledUnitInstall(
                        fraise_name=fraise_name,
                        environment=environment,
                        job_name=job_name,
                        unit_name=unit_name,
                        is_timer=is_timer,
                        source_path=app_path / APP_PATH_UNITS_SUBDIR / unit_name,
                        dest_path=SYSTEMD_DEST_DIR / unit_name,
                        app_path=app_path,
                    )
                )
    return units


def classify_unit(install: ScheduledUnitInstall) -> UnitDiff:
    """Compare ``install.source_path`` against ``install.dest_path``; no writes.

    Returns:
        - ``MISSING_SOURCE`` if ``source_path`` does not exist.
        - ``ABSENT`` if ``dest_path`` does not exist (source does).
        - ``IDENTICAL`` if both exist and are byte-equal.
        - ``DRIFTED`` if both exist and differ — with a short one-line summary.
    """
    if not install.source_path.exists():
        return UnitDiff(install, UnitState.MISSING_SOURCE, None)
    if not install.dest_path.exists():
        return UnitDiff(install, UnitState.ABSENT, None)
    src_bytes = install.source_path.read_bytes()
    dst_bytes = install.dest_path.read_bytes()
    if src_bytes == dst_bytes:
        return UnitDiff(install, UnitState.IDENTICAL, None)
    summary = _short_diff_summary(src_bytes, dst_bytes)
    return UnitDiff(install, UnitState.DRIFTED, summary)


def _short_diff_summary(src: bytes, dst: bytes) -> str:
    """One-line summary of byte differences between source and dest unit files.

    Counts added / removed lines from a unified diff (dest → source perspective,
    so "added" means lines present in source but not dest). The full unified
    diff is only emitted by the CLI under ``--verbose`` — keep the summary tight.
    """
    src_lines = src.decode("utf-8", errors="replace").splitlines()
    dst_lines = dst.decode("utf-8", errors="replace").splitlines()
    diff = list(difflib.unified_diff(dst_lines, src_lines, lineterm=""))
    added = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
    return f"unit body differs ({added} lines added, {removed} removed)"


@dataclass(frozen=True)
class ApplyReport:
    """Summary of what ``apply_unit_diffs`` actually did during one call.

    ``rejected_reason``, ``busy``, ``timed_out`` are populated by
    ``apply_unit_diffs_via_helper`` (#240 Phase 6); the direct apply path
    leaves them at their defaults.
    """

    written: tuple[ScheduledUnitInstall, ...]  # ABSENT + (DRIFTED with force)
    skipped_identical: tuple[ScheduledUnitInstall, ...]
    enabled_timers: tuple[ScheduledUnitInstall, ...]
    reloaded: bool
    rejected_reason: str | None = None
    busy: bool = False
    timed_out: bool = False


def _validate_unit_path_safety(
    install: ScheduledUnitInstall,
    *,
    systemd_dest_dir: Path,
) -> None:
    """Raise if the unit name, dest path, or source path could escape sandboxes.

    Three guards, all evaluated before any filesystem mutation:

    1. ``unit_name`` must not contain ``/`` or ``..``. ``validate_service_name``'s
       regex ``^[a-zA-Z0-9_@.\\-]+$`` rejects ``/`` but accepts ``..`` (any run
       of dots passes), so we add the explicit substring check here.
    2. ``dest_path.parent`` must resolve to ``systemd_dest_dir`` — catches a
       caller that constructed a ``ScheduledUnitInstall`` with a tampered
       ``dest_path``.
    3. ``source_path`` (resolved through any symlinks) must be contained under
       ``app_path/scripts/systemd``. Blocks a hostile worktree from symlinking
       e.g. ``scripts/systemd/foo.timer`` to ``/etc/passwd``.
    """
    if "/" in install.unit_name or ".." in install.unit_name:
        msg = f"unsafe unit name {install.unit_name!r}: contains '/' or '..'"
        raise ScheduledInstallError(msg)

    expected_dest_parent = systemd_dest_dir.resolve()
    actual_dest_parent = install.dest_path.parent.resolve()
    if actual_dest_parent != expected_dest_parent:
        msg = f"dest path {install.dest_path} escapes systemd dir {systemd_dest_dir}"
        raise ScheduledInstallError(msg)

    source_root = (install.app_path / APP_PATH_UNITS_SUBDIR).resolve()
    if not install.source_path.exists():
        # No symlink to resolve; MISSING_SOURCE will fire separately.
        return
    actual_source = install.source_path.resolve()
    if not actual_source.is_relative_to(source_root):
        msg = (
            f"source path {install.source_path} (resolves to {actual_source}) "
            f"escapes {source_root}"
        )
        raise ScheduledInstallError(msg)


def apply_unit_diffs(
    diffs: list[UnitDiff],
    *,
    runner: CommandRunner,
    force: bool = False,
    systemd_dest_dir: Path | None = None,
) -> ApplyReport:
    """Converge the dest filesystem with the source. The only write-the-FS path.

    Order of operations — all guards raise *before* any mutation:

    1. Path-traversal checks on every diff (rejects ``/``, ``..``, dest tamper,
       symlink-escape on source).
    2. ``MISSING_SOURCE`` → raise ``ScheduledInstallError``.
    3. ``DRIFTED`` without ``force=True`` → raise ``ScheduledInstallError``.
    4. Copy each ``ABSENT`` (and each ``DRIFTED`` if ``force``) source → dest,
       chmod 0o644.
    5. If any writes happened, run ``systemctl daemon-reload`` once.
    6. For each timer that was written, run ``systemctl enable --now <unit>``.
       (``IDENTICAL`` timers are NOT re-enabled — that would defeat the
       zero-side-effect re-run invariant.)

    Note on ``sudo``: this function does NOT prefix systemctl invocations with
    ``sudo``. The privilege model (Open Question #2) is that the operator
    invokes the whole command via ``sudo fraisier scheduled-install``, so the
    Python process is already root by the time we get here.
    """
    dest_dir = systemd_dest_dir if systemd_dest_dir is not None else SYSTEMD_DEST_DIR

    for diff in diffs:
        _validate_unit_path_safety(diff.install, systemd_dest_dir=dest_dir)

    missing = [d for d in diffs if d.state is UnitState.MISSING_SOURCE]
    if missing:
        names = ", ".join(d.install.unit_name for d in missing)
        msg = (
            f"source not found for: {names}. "
            "Did the deploy land in app_path/scripts/systemd/?"
        )
        raise ScheduledInstallError(msg)

    if not force:
        drifted = [d for d in diffs if d.state is UnitState.DRIFTED]
        if drifted:
            names = ", ".join(d.install.unit_name for d in drifted)
            msg = (
                f"drifted units (pass --force to overwrite): {names}. "
                "These dest files differ from source; an operator likely "
                "hand-edited them."
            )
            raise ScheduledInstallError(msg)

    written: list[ScheduledUnitInstall] = []
    skipped_identical: list[ScheduledUnitInstall] = []
    for diff in diffs:
        if diff.state is UnitState.ABSENT or (
            diff.state is UnitState.DRIFTED and force
        ):
            shutil.copy2(diff.install.source_path, diff.install.dest_path)
            diff.install.dest_path.chmod(0o644)
            written.append(diff.install)
        elif diff.state is UnitState.IDENTICAL:
            skipped_identical.append(diff.install)

    reloaded = False
    if written:
        runner.run(["systemctl", "daemon-reload"])
        reloaded = True

    enabled: list[ScheduledUnitInstall] = []
    for install in written:
        if install.is_timer:
            runner.run(["systemctl", "enable", "--now", install.unit_name])
            enabled.append(install)

    return ApplyReport(
        written=tuple(written),
        skipped_identical=tuple(skipped_identical),
        enabled_timers=tuple(enabled),
        reloaded=reloaded,
    )


# ---------------------------------------------------------------------------
# #240 Phase 6 — apply_unit_diffs_via_helper (client for unit-installer socket)
# ---------------------------------------------------------------------------


def apply_unit_diffs_via_helper(
    diffs: list[UnitDiff],
    *,
    socket_path: Path,
    force: bool = False,
    write_markers: bool = False,
    config_path: Path | None = None,
) -> ApplyReport:
    """Apply ``diffs`` by sending a manifest to the unit-installer helper.

    Parallel to :func:`apply_unit_diffs` but goes through #240's socket
    helper instead of writing to disk directly. The helper enforces
    SO_PEERCRED, an allowlist (baked at scaffold-render time), and
    TOCTOU realpath checks; this client does client-side fast-fail
    validation (same path-safety checks) before the round-trip.

    Args:
        diffs: Unit diffs to converge.
        socket_path: Path of the helper's listening socket.
        force: Overwrite DRIFTED units (mirrors apply_unit_diffs's ``force``).
        write_markers: If True, include a marker payload on each install_file
            op so the helper writes a .fraisier-managed sidecar (consumed by
            #240's prune planner).
        config_path: ``fraises.yaml`` path; required when ``write_markers`` is
            True. The client resolves it via ``Path.resolve(strict=True)``
            before sending so the marker's ``fraises_yaml_path`` is absolute.

    Returns:
        ``ApplyReport`` populated from the helper's structured response.
        On ``rejected`` / ``busy`` / ``timeout`` responses the corresponding
        field is set and ``written`` is an empty tuple.

    Raises:
        ScheduledInstallError: If client-side path-safety validation fails
            (same conditions as apply_unit_diffs's _validate_unit_path_safety).
    """
    import socket as _socket

    # Same client-side fast-fail as the direct apply path.
    for diff in diffs:
        _validate_unit_path_safety(diff.install, systemd_dest_dir=SYSTEMD_DEST_DIR)

    missing = [d for d in diffs if d.state is UnitState.MISSING_SOURCE]
    if missing:
        names = ", ".join(d.install.unit_name for d in missing)
        msg = f"source not found for: {names}"
        raise ScheduledInstallError(msg)

    if not force:
        drifted = [d for d in diffs if d.state is UnitState.DRIFTED]
        if drifted:
            names = ", ".join(d.install.unit_name for d in drifted)
            msg = (
                f"drifted units (pass force=True to overwrite): {names}. "
                "Helper would have rejected the manifest at validate."
            )
            raise ScheduledInstallError(msg)

    resolved_config_path: Path | None = None
    if write_markers:
        if config_path is None:
            msg = "write_markers=True requires config_path"
            raise ScheduledInstallError(msg)
        resolved_config_path = config_path.resolve(strict=True)

    manifest = _build_helper_manifest(
        diffs,
        force=force,
        resolved_config_path=resolved_config_path,
    )

    if not manifest.operations:
        # Nothing to do — no need to even open the socket.
        return ApplyReport(
            written=(),
            skipped_identical=tuple(
                d.install for d in diffs if d.state is UnitState.IDENTICAL
            ),
            enabled_timers=(),
            reloaded=False,
        )

    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.connect(str(socket_path))
    try:
        return _apply_via_open_socket(sock, manifest, diffs)
    finally:
        sock.close()


def _apply_via_open_socket(
    sock: Any, manifest: Manifest, diffs: list[UnitDiff]
) -> ApplyReport:
    """Send ``manifest`` on an already-connected ``sock`` and parse the report.

    Factored from ``apply_unit_diffs_via_helper`` so tests can drive the
    socket layer with a ``socket.socketpair`` (bypassing the connect step,
    which AF_UNIX socketpair sockets reject as "already connected").
    """
    response = _exchange_manifest(sock, manifest)
    return _build_apply_report(response, diffs)


def _build_helper_manifest(
    diffs: list[UnitDiff],
    *,
    force: bool,
    resolved_config_path: Path | None,
) -> Manifest:
    """Construct a manifest from a list of ``UnitDiff``s.

    Three op kinds emitted:

    - ``ABSENT`` + (``DRIFTED`` with ``force``) → ``InstallFileOp``.
    - ``IDENTICAL`` with marker missing on disk → ``WriteMarkerOp``
      (auto-backfill migration for v0.28.0-installed units; Phase 0
      decision #2 of #240 follow-up 04). Idempotent on re-run: once the
      marker exists no op is emitted for that diff.
    - All other ``IDENTICAL`` → skipped, no round-trip.

    Each install_file (not write_marker) emits a ``daemon_reload``
    post-action exactly once (deduplicated) and an ``enable_now`` per
    ``.timer`` op — write_marker only writes the sidecar; the unit is
    already installed and active.
    """
    from datetime import datetime

    from fraisier.unit_installer_protocol import (
        DaemonReloadAction,
        EnableNowAction,
        InstallFileOp,
        Manifest,
        MarkerMeta,
        WriteMarkerOp,
    )

    operations: list = []
    enable_actions: list[EnableNowAction] = []
    install_file_count = 0
    for diff in diffs:
        if diff.state is UnitState.MISSING_SOURCE:
            continue  # already raised
        if diff.state is UnitState.DRIFTED and not force:
            continue  # already raised above; defence-in-depth

        marker: MarkerMeta | None = None
        if resolved_config_path is not None:
            marker = MarkerMeta(
                fraises_yaml_path=str(resolved_config_path),
                fraise_name=diff.install.fraise_name,
                environment=diff.install.environment,
                job_name=diff.install.job_name,
            )

        if diff.state is UnitState.IDENTICAL:
            if (
                marker is not None
                and not marker_path_for(diff.install.dest_path).exists()
            ):
                operations.append(
                    WriteMarkerOp(dest_path=str(diff.install.dest_path), marker=marker)
                )
            continue

        # ABSENT or DRIFTED+force → install_file
        operations.append(
            InstallFileOp(
                source_path=str(diff.install.source_path),
                dest_path=str(diff.install.dest_path),
                mode="0644",
                force=force,
                marker=marker,
            )
        )
        install_file_count += 1
        if diff.install.is_timer:
            enable_actions.append(EnableNowAction(unit=diff.install.unit_name))

    post_actions: list = []
    if install_file_count > 0:
        post_actions.append(DaemonReloadAction())
    post_actions.extend(enable_actions)

    deploy_id = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "-via-helper"
    return Manifest(
        version=1,
        deploy_id=deploy_id,
        operations=tuple(operations),
        post_actions=tuple(post_actions),
    )


def _exchange_manifest(sock: Any, manifest: Manifest) -> dict:
    """Send ``manifest`` over ``sock`` and return the parsed JSON response."""
    import contextlib
    import json as _json

    from fraisier.unit_installer_protocol import serialize_manifest

    sock.sendall(serialize_manifest(manifest))
    with contextlib.suppress(OSError):
        sock.shutdown(1)  # SHUT_WR — signal we're done sending

    raw = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        raw.extend(chunk)
        if b"\n" in raw:
            break
    return _json.loads(bytes(raw))


def _build_apply_report(response: dict, diffs: list[UnitDiff]) -> ApplyReport:
    """Translate a helper response dict into an ``ApplyReport``."""
    status = response.get("status")
    skipped_identical = tuple(
        d.install for d in diffs if d.state is UnitState.IDENTICAL
    )
    if status == "ok":
        installed_basenames = set(response.get("installed", []))
        written = tuple(
            d.install for d in diffs if d.install.unit_name in installed_basenames
        )
        enabled = tuple(install for install in written if install.is_timer)
        reloaded = any(
            a.get("kind") == "daemon-reload" for a in response.get("post_actions", [])
        )
        return ApplyReport(
            written=written,
            skipped_identical=skipped_identical,
            enabled_timers=enabled,
            reloaded=reloaded,
        )
    if status == "rejected":
        return ApplyReport(
            written=(),
            skipped_identical=skipped_identical,
            enabled_timers=(),
            reloaded=False,
            rejected_reason=response.get("reason", "(no reason provided)"),
        )
    if status == "busy":
        return ApplyReport(
            written=(),
            skipped_identical=skipped_identical,
            enabled_timers=(),
            reloaded=False,
            busy=True,
            rejected_reason=response.get("reason"),
        )
    if status == "timeout":
        return ApplyReport(
            written=(),
            skipped_identical=skipped_identical,
            enabled_timers=(),
            reloaded=False,
            timed_out=True,
            rejected_reason=response.get("reason"),
        )
    msg = f"helper returned unknown status: {status!r}"
    raise ScheduledInstallError(msg)


# ---------------------------------------------------------------------------
# #240 follow-up 04 Phase 1 — marker convention (read helpers)
# ---------------------------------------------------------------------------


class CorruptMarker(Exception):
    """Raised by ``read_marker`` when a marker file can't be parsed.

    The prune planner catches this and converts to a ``stale_marker`` plan
    so the operator can clean up by-product without manual intervention.
    """


def marker_path_for(unit_dest: Path) -> Path:
    """Return the sidecar path for ``unit_dest`` (``<unit>.fraisier-managed``)."""
    return unit_dest.with_name(unit_dest.name + MARKER_SUFFIX)


def build_marker(
    install: ScheduledUnitInstall, *, resolved_config_path: Path
) -> MarkerMeta:
    """Build a ``MarkerMeta`` for ``install`` from a pre-resolved config path.

    ``resolved_config_path`` MUST be absolute — the caller is responsible for
    calling ``Path.resolve(strict=True)`` BEFORE invoking this. The marker's
    ``fraises_yaml_path`` carries the absolute form so prune planners
    launched from different working directories converge on the same
    project identity (see ``MarkerMeta`` docstring).
    """
    from fraisier.unit_installer_protocol import MarkerMeta as _MarkerMeta

    if not resolved_config_path.is_absolute():
        msg = (
            f"build_marker requires an absolute config path; got "
            f"{resolved_config_path!r}"
        )
        raise ScheduledInstallError(msg)
    return _MarkerMeta(
        fraises_yaml_path=str(resolved_config_path),
        fraise_name=install.fraise_name,
        environment=install.environment,
        job_name=install.job_name,
    )


def read_marker(marker_path: Path) -> MarkerMeta:
    """Parse a marker file on disk into a ``MarkerMeta``.

    Raises ``CorruptMarker`` on JSON decode failure, missing required fields,
    or OSError reading the file. The prune planner catches this and tags
    the marker as ``stale_marker`` so the operator can clean up.
    """
    import json as _json

    from fraisier.unit_installer_protocol import MarkerMeta as _MarkerMeta

    try:
        raw = marker_path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read marker {marker_path}: {exc}"
        raise CorruptMarker(msg) from exc
    try:
        payload = _json.loads(raw)
    except _json.JSONDecodeError as exc:
        msg = f"marker {marker_path} is not valid JSON: {exc}"
        raise CorruptMarker(msg) from exc
    if not isinstance(payload, dict):
        msg = f"marker {marker_path} is not a JSON object"
        raise CorruptMarker(msg)
    required = ("fraises_yaml_path", "fraise_name", "environment", "job_name")
    missing = [k for k in required if k not in payload]
    if missing:
        msg = f"marker {marker_path} missing required fields: {missing}"
        raise CorruptMarker(msg)
    return _MarkerMeta(
        fraises_yaml_path=payload["fraises_yaml_path"],
        fraise_name=payload["fraise_name"],
        environment=payload["environment"],
        job_name=payload["job_name"],
    )


def find_markers(systemd_dest_dir: Path) -> list[Path]:
    """Return every ``*.fraisier-managed`` path under ``systemd_dest_dir``.

    Defence-in-depth: a marker whose paired unit name would fail
    ``validate_service_name`` is silently skipped (the marker's existence is
    advisory; if its name is malformed someone planted it manually). The
    skip prevents future shell-injection vectors if the unit name flows into
    a systemctl invocation downstream.
    """
    if not systemd_dest_dir.exists():
        return []
    markers: list[Path] = []
    for entry in systemd_dest_dir.iterdir():
        if not entry.name.endswith(MARKER_SUFFIX):
            continue
        unit_name = entry.name[: -len(MARKER_SUFFIX)]
        try:
            validate_service_name(unit_name)
        except ValueError:
            continue
        # Reject .. and leading dot at the unit-name layer too (paired check
        # with _validate_unit_path_safety).
        if ".." in unit_name or unit_name.startswith("."):
            continue
        markers.append(entry)
    return sorted(markers)

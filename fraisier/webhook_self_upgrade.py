"""Webhook-driven self-upgrade for fraisier (issue #162).

When a deployed pyproject.toml pins a newer fraisier than the webhook is
running, ``maybe_self_upgrade`` detaches a worker subprocess that:

1. runs ``uv tool install --force --refresh-package fraisier fraisier=={X}``
   against the webhook user's own uv tool dir, then
2. on success, sends a ``restart`` RPC to the systemctl-helper socket
   (``FRAISIER_SYSTEMCTL_SOCKET``) for the webhook's own service unit.

The worker is spawned with ``start_new_session=True`` so it survives the
webhook restart that follows step 2. The webhook's own service unit must
appear in the systemctl-helper allowlist (added in Phase 3); without it
the restart RPC is rejected with ``service not allowed``.

Mirrors the operator-driven path in :mod:`fraisier.bootstrap` (commit
``590e31a``), which covers the workstation-side upgrade.
"""

from __future__ import annotations

import argparse
import importlib.metadata as importlib_metadata
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fraisier.service_managers.systemd import _call_via_socket
from fraisier.versioning import detect_required_fraisier_version

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

log = logging.getLogger(__name__)

# Webhook units run with ProtectSystem=strict and have /var/lib/fraisier in
# ReadWritePaths, so a sibling directory is writable by the deploy user.
_LOG_DIR = Path("/var/lib/fraisier/self-upgrade")


def _build_install_cmd(required: str) -> list[str]:
    """Return the argv for ``uv tool install`` matching bootstrap's form."""
    return [
        "uv",
        "tool",
        "install",
        "--force",
        "--refresh-package",
        "fraisier",
        f"fraisier=={required}",
    ]


def _parse_semver(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"not semver: {version!r}")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def _preflight_helper_allowlist(socket_path: str, service: str) -> str | None:
    """Detect a known-bad systemctl-helper allowlist before spawning the worker.

    Sends a read-only ``is-active`` RPC; if the helper rejects with
    ``service not allowed``, returns the rejection reason. All other outcomes
    (no socket configured, connection refused, service inactive, etc.) return
    None — those are non-actionable here and let the worker proceed with its
    own logging.

    This pre-flight exists because ``_spawn_upgrade`` detaches the worker that
    actually does the restart RPC, so its failure is recorded only under
    ``/var/lib/fraisier/self-upgrade/`` and never reaches the webhook's main
    journal (issue #218). Hoisting the allowlist check into the parent surfaces
    the most common scaffold-staleness case where operators are looking.
    """
    if not socket_path:
        return None
    try:
        _call_via_socket(socket_path, "is-active", service, check=True)
    except ConnectionRefusedError:
        return None
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        if "not allowed" in stderr.lower():
            return stderr
    return None


def maybe_self_upgrade(
    app_path: Path,
    *,
    project_name: str,
    enabled: bool,
    spawn: Callable[[str, str], None] | None = None,
) -> None:
    """Best-effort: detect a newer pinned fraisier and spawn a detached upgrade.

    Never raises — a failure here must not break a successful deploy. The
    *spawn* parameter is a test seam; production code leaves it at None and
    uses :func:`_spawn_upgrade`.
    """
    if not enabled:
        return
    try:
        required = detect_required_fraisier_version(app_path)
        if required is None:
            return
        installed = importlib_metadata.version("fraisier")
        try:
            if _parse_semver(required) <= _parse_semver(installed):
                return
        except ValueError:
            log.warning(
                "self-upgrade: skipping — non-semver version comparison "
                "(required=%s installed=%s)",
                required,
                installed,
            )
            return
        socket_path = os.environ.get("FRAISIER_SYSTEMCTL_SOCKET", "")
        service = f"fraisier-{project_name}-webhook.service"
        rejection = _preflight_helper_allowlist(socket_path, service)
        if rejection is not None:
            log.warning(
                "self-upgrade: skipping upgrade to %s — systemctl helper "
                "rejected pre-flight: %s. The webhook unit is missing from "
                "the helper allowlist (typically because this host was "
                "scaffolded before fraisier 0.18.0). Re-run "
                "`fraisier scaffold && fraisier scaffold-install --yes` to "
                "refresh it, then the next deploy will self-upgrade.",
                required,
                rejection,
            )
            return
        log.info(
            "self-upgrade: required=%s installed=%s — spawning upgrade for %s",
            required,
            installed,
            project_name,
        )
        (spawn or _spawn_upgrade)(required, project_name)
    except Exception:
        log.exception("self-upgrade: skipped due to unexpected error")


def _open_log_fd(project_name: str):
    """Open a log file under :data:`_LOG_DIR`. Fall back to DEVNULL on failure."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        return (_LOG_DIR / f"{project_name}-{ts}.log").open("ab")
    except OSError as exc:
        log.warning("self-upgrade: could not open log file (%s); using DEVNULL", exc)
        return subprocess.DEVNULL


def _spawn_upgrade(required: str, project_name: str) -> None:
    """Spawn the detached worker that runs install + restart-RPC."""
    socket_path = os.environ.get("FRAISIER_SYSTEMCTL_SOCKET", "")
    service = f"fraisier-{project_name}-webhook.service"
    cmd = [
        sys.executable,
        "-m",
        "fraisier.webhook_self_upgrade",
        "--required",
        required,
        "--service",
        service,
        "--socket",
        socket_path,
    ]
    stdout = _open_log_fd(project_name)
    subprocess.Popen(
        cmd,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.STDOUT,
    )


def _run_upgrade(required: str, service: str, socket_path: str) -> int:
    """Synchronous worker — runs install, then restart RPC on success."""
    cmd = _build_install_cmd(required)
    log.info("self-upgrade: running %s", " ".join(cmd))
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(
            "self-upgrade: install failed rc=%s stderr=%s",
            result.returncode,
            result.stderr,
        )
        return result.returncode
    log.info("self-upgrade: install succeeded; requesting restart of %s", service)
    if not socket_path:
        log.warning(
            "self-upgrade: FRAISIER_SYSTEMCTL_SOCKET not set; skipping restart of %s",
            service,
        )
        return 0
    try:
        _call_via_socket(socket_path, "restart", service)
    except (ConnectionRefusedError, subprocess.CalledProcessError) as exc:
        log.error("self-upgrade: restart RPC failed: %s", exc)
        return 1
    return 0


def _main() -> None:
    parser = argparse.ArgumentParser(prog="fraisier.webhook_self_upgrade")
    parser.add_argument("--required", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--socket", default="")
    args = parser.parse_args()
    sys.exit(_run_upgrade(args.required, args.service, args.socket))


if __name__ == "__main__":  # pragma: no cover
    _main()

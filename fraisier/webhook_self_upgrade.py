"""Webhook-driven self-upgrade for fraisier (issues #162, #246).

When a deployed pyproject.toml pins a newer fraisier than the webhook is
running, ``maybe_self_upgrade`` detaches a worker subprocess that:

1. touches the ``.draining`` flag in ``lock_dir`` so any new deploy hitting
   the webhook during the upgrade window is refused with HTTP 503 +
   ``Retry-After`` rather than being silently killed by the restart RPC,
2. runs ``uv tool install --force --refresh-package fraisier fraisier=={X}``
   against the webhook user's own uv tool dir,
3. sleeps a short *settle* delay so any deploy accepted in the small window
   between dispatch acceptance and lock acquisition reaches its
   ``with deployment_lock(...)`` line before the worker counts in-flight
   locks,
4. polls ``count_held_deployment_locks`` until it reaches 0 or the drain
   timeout expires,
5. clears the flag and sends a ``restart`` RPC to the systemctl-helper
   socket (``FRAISIER_SYSTEMCTL_SOCKET``) for the webhook's own service
   unit.

The worker is spawned with ``start_new_session=True`` so it survives the
webhook restart that follows step 5. The webhook's own service unit must
appear in the systemctl-helper allowlist; without it the restart RPC is
rejected with ``service not allowed`` (pre-flighted before spawn).

Scope: the drain coordination is correct for ``lock_backend=file`` (the
default). On ``lock_backend=database`` hosts the drain loop sees no
``*.lock`` files and immediately proceeds to restart — matching today's
behaviour for that backend.

Operator docs (knobs, failure modes, residual-race scope, optional GH
Actions retry snippet): ``docs/operations/self-upgrade.md``.
"""

from __future__ import annotations

import argparse
import importlib.metadata as importlib_metadata
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fraisier.config import get_config
from fraisier.locking import DRAINING_FLAG_NAME, count_held_deployment_locks
from fraisier.service_managers.systemd import _call_via_socket
from fraisier.versioning import detect_required_fraisier_version

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable, Iterator

log = logging.getLogger(__name__)

# Webhook units run with ProtectSystem=strict and have /var/lib/fraisier in
# ReadWritePaths, so a sibling directory is writable by the deploy user.
_LOG_DIR = Path("/var/lib/fraisier/self-upgrade")

# Distinct from install-failure rc=1 / restart-RPC-failure rc=1 so operators
# scanning the per-event log can tell drain-timeout apart at a glance.
_DRAIN_TIMEOUT_RC = 2

_DEFAULT_DRAIN_TIMEOUT_S = 600
_DEFAULT_DRAIN_POLL_S = 1.0
_DEFAULT_DRAIN_SETTLE_S = 2.0
_DEFAULT_LOCK_DIR = "/run/fraisier"


@dataclass(frozen=True)
class _DrainResult:
    drained: bool
    held: list[str]


@dataclass(frozen=True)
class _SpawnArgs:
    socket_path: str
    lock_dir: str
    drain_timeout_s: int
    drain_poll_s: float
    drain_settle_s: float


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


def _resolve_spawn_args() -> _SpawnArgs:
    """Read ``webhook.*`` knobs + ``deployment.lock_dir`` from config.

    All four ``webhook.self_upgrade_*`` keys are read via ``.get(name, default)``
    on the raw ``dict[str, Any]`` returned by ``FraisierConfig.webhook`` — no
    ``WebhookConfig`` dataclass is introduced for this fix. A missing
    ``fraises.yaml`` (``FileNotFoundError``) reverts to safe defaults so the
    operator-invoked path keeps working.
    """
    socket_path = os.environ.get("FRAISIER_SYSTEMCTL_SOCKET", "")
    try:
        config = get_config()
    except FileNotFoundError:
        return _SpawnArgs(
            socket_path=socket_path,
            lock_dir=_DEFAULT_LOCK_DIR,
            drain_timeout_s=_DEFAULT_DRAIN_TIMEOUT_S,
            drain_poll_s=_DEFAULT_DRAIN_POLL_S,
            drain_settle_s=_DEFAULT_DRAIN_SETTLE_S,
        )
    webhook_cfg = config.webhook
    lock_dir = getattr(config.deployment, "lock_dir", _DEFAULT_LOCK_DIR)
    return _SpawnArgs(
        socket_path=socket_path,
        lock_dir=str(lock_dir),
        drain_timeout_s=int(
            webhook_cfg.get("self_upgrade_drain_timeout_s", _DEFAULT_DRAIN_TIMEOUT_S)
        ),
        drain_poll_s=float(
            webhook_cfg.get("self_upgrade_drain_poll_s", _DEFAULT_DRAIN_POLL_S)
        ),
        drain_settle_s=float(
            webhook_cfg.get("self_upgrade_drain_settle_s", _DEFAULT_DRAIN_SETTLE_S)
        ),
    )


def _spawn_upgrade(required: str, project_name: str) -> None:
    """Spawn the detached worker that runs install + drain + restart-RPC."""
    spawn_args = _resolve_spawn_args()
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
        spawn_args.socket_path,
        "--lock-dir",
        spawn_args.lock_dir,
        "--drain-timeout",
        str(spawn_args.drain_timeout_s),
        "--drain-poll",
        str(spawn_args.drain_poll_s),
        "--drain-settle",
        str(spawn_args.drain_settle_s),
    ]
    stdout = _open_log_fd(project_name)
    subprocess.Popen(
        cmd,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.STDOUT,
    )


@contextmanager
def _with_draining_flag(lock_dir: Path) -> Iterator[Path]:
    """Touch ``{lock_dir}/.draining`` on entry; always unlink on exit."""
    flag = lock_dir / DRAINING_FLAG_NAME
    flag.touch()
    try:
        yield flag
    finally:
        flag.unlink(missing_ok=True)


def _held_lock_basenames(lock_dir: Path) -> list[str]:
    """Names of ``*.lock`` files currently present — used for timeout logging."""
    if not lock_dir.exists():
        return []
    return sorted(p.name for p in lock_dir.glob("*.lock"))


def _wait_for_deploys_to_drain(
    lock_dir: Path,
    timeout_s: float,
    poll_s: float,
) -> _DrainResult:
    """Block until no ``*.lock`` is flock'd, or the deadline expires.

    Returns a :class:`_DrainResult` so callers can log which locks are still
    held on timeout (the helper does not parse ``/proc/locks`` for holder
    PIDs — basenames are enough to identify which fraise hung).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        held = count_held_deployment_locks(lock_dir)
        if held == 0:
            return _DrainResult(drained=True, held=[])
        if time.monotonic() >= deadline:
            return _DrainResult(drained=False, held=_held_lock_basenames(lock_dir))
        time.sleep(poll_s)


def _send_restart(socket_path: str, service: str) -> int:
    """Send the ``restart`` RPC, returning the same rc shape today's code does."""
    try:
        _call_via_socket(socket_path, "restart", service)
    except (ConnectionRefusedError, subprocess.CalledProcessError) as exc:
        log.error("self-upgrade: restart RPC failed: %s", exc)
        return 1
    return 0


def _run_install(required: str) -> int:
    """Run the install command, log failure, return rc."""
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


def _run_upgrade(
    required: str,
    service: str,
    socket_path: str,
    *,
    lock_dir: Path | None = None,
    drain_timeout_s: int = _DEFAULT_DRAIN_TIMEOUT_S,
    drain_poll_s: float = _DEFAULT_DRAIN_POLL_S,
    drain_settle_s: float = _DEFAULT_DRAIN_SETTLE_S,
) -> int:
    """Coordinated worker — flag → install → settle → drain → restart.

    With ``lock_dir=None`` (operator-invoked ``_main`` or a config edge case)
    the worker falls back to today's behaviour: install, then immediate
    restart, with a loud WARNING so the missing coordination is visible.
    """
    if lock_dir is None:
        log.warning(
            "self-upgrade: lock_dir unresolved; running install + restart "
            "without drain coordination"
        )
        rc = _run_install(required)
        if rc != 0:
            return rc
        if not socket_path:
            log.warning(
                "self-upgrade: FRAISIER_SYSTEMCTL_SOCKET not set; "
                "skipping restart of %s",
                service,
            )
            return 0
        return _send_restart(socket_path, service)

    # Flag covers install + drain so dispatch refuses new deploys for the
    # entire upgrade window, not just the drain tail.
    with _with_draining_flag(lock_dir):
        rc = _run_install(required)
        if rc != 0:
            return rc
        if not socket_path:
            log.warning(
                "self-upgrade: FRAISIER_SYSTEMCTL_SOCKET not set; "
                "skipping restart of %s",
                service,
            )
            return 0
        # Brief settle so any deploy accepted in the dispatch→lock window
        # reaches `with deployment_lock(...)` before we count.
        time.sleep(drain_settle_s)
        result = _wait_for_deploys_to_drain(lock_dir, drain_timeout_s, drain_poll_s)
        if not result.drained:
            log.warning(
                "self-upgrade: drain timeout (%ds) — held locks: %s; "
                "skipping restart. Operator must restart %s manually.",
                drain_timeout_s,
                ", ".join(result.held),
                service,
            )
            return _DRAIN_TIMEOUT_RC

    log.info("self-upgrade: deploys drained; requesting restart of %s", service)
    return _send_restart(socket_path, service)


def _main() -> None:
    parser = argparse.ArgumentParser(prog="fraisier.webhook_self_upgrade")
    parser.add_argument("--required", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--socket", default="")
    parser.add_argument("--lock-dir", default="", dest="lock_dir")
    parser.add_argument(
        "--drain-timeout",
        type=int,
        default=_DEFAULT_DRAIN_TIMEOUT_S,
        dest="drain_timeout",
    )
    parser.add_argument(
        "--drain-poll",
        type=float,
        default=_DEFAULT_DRAIN_POLL_S,
        dest="drain_poll",
    )
    parser.add_argument(
        "--drain-settle",
        type=float,
        default=_DEFAULT_DRAIN_SETTLE_S,
        dest="drain_settle",
    )
    args = parser.parse_args()
    lock_dir = Path(args.lock_dir) if args.lock_dir else None
    sys.exit(
        _run_upgrade(
            args.required,
            args.service,
            args.socket,
            lock_dir=lock_dir,
            drain_timeout_s=args.drain_timeout,
            drain_poll_s=args.drain_poll,
            drain_settle_s=args.drain_settle,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    _main()

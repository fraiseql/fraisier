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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fraisier.config import get_config
from fraisier.drain_restart import (
    DEFAULT_DRAIN_POLL_S,
    DEFAULT_DRAIN_SETTLE_S,
    DEFAULT_DRAIN_TIMEOUT_S,
    DEFAULT_LOCK_DIR,
    DRAIN_TIMEOUT_RC,
    DrainResult,
    draining_flag,
    held_lock_basenames,
    send_restart,
    wait_for_deploys_to_drain,
)
from fraisier.self_upgrade_record import (
    clear_self_upgrade_failure,
    read_self_upgrade_failure,
    record_self_upgrade_failure,
)
from fraisier.service_managers.systemd import _call_via_socket
from fraisier.versioning import detect_required_fraisier_version
from fraisier.worker_logging import (
    SELF_UPGRADE_LOG_DIR,
    configure_worker_logging,
    open_worker_log,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

log = logging.getLogger(__name__)

#: Rebindable alias of :data:`~fraisier.worker_logging.SELF_UPGRADE_LOG_DIR`,
#: kept so tests can point the worker at a temporary tree without moving the
#: directory the refusal message names.
_LOG_DIR = SELF_UPGRADE_LOG_DIR

#: Where the worker resolves its own unit's ``ExecStart=``. A module constant so
#: tests can point it at a temporary tree.
_UNIT_DIR = Path("/etc/systemd/system")

#: The install ran but left the unit unable to start, so no restart was sent.
#: Distinct from the install's own rc: the operator's next move is different.
ENTRYPOINT_BROKEN_RC = 4

# Re-exported under their historical private names: this module is where the
# drain sequence was written, and its tests and callers still reach for these
# spellings. The implementation now lives in `drain_restart` so the deferred
# restarts recorded by install.sh share it rather than growing a second copy.
_DRAIN_TIMEOUT_RC = DRAIN_TIMEOUT_RC
_DEFAULT_DRAIN_TIMEOUT_S = DEFAULT_DRAIN_TIMEOUT_S
_DEFAULT_DRAIN_POLL_S = DEFAULT_DRAIN_POLL_S
_DEFAULT_DRAIN_SETTLE_S = DEFAULT_DRAIN_SETTLE_S
_DEFAULT_LOCK_DIR = DEFAULT_LOCK_DIR
_DrainResult = DrainResult
_with_draining_flag = draining_flag
_held_lock_basenames = held_lock_basenames
_wait_for_deploys_to_drain = wait_for_deploys_to_drain
_send_restart = send_restart


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
) -> bool:
    """Best-effort: detect a newer pinned fraisier and spawn a detached upgrade.

    Returns True iff an upgrade worker was spawned. Callers use that to avoid
    starting a second drain worker: both raise the same single ``.draining``
    flag, and the first to finish would unlink it out from under the other
    (#349).

    Never raises — a failure here must not break a successful deploy. The
    *spawn* parameter is a test seam; production code leaves it at None and
    uses :func:`_spawn_upgrade`.
    """
    if not enabled:
        return False
    try:
        required = detect_required_fraisier_version(app_path)
        if required is None:
            return False
        installed = importlib_metadata.version("fraisier")
        try:
            if _parse_semver(required) <= _parse_semver(installed):
                return False
        except ValueError:
            log.warning(
                "self-upgrade: skipping — non-semver version comparison "
                "(required=%s installed=%s)",
                required,
                installed,
            )
            return False
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
            return False
        log.info(
            "self-upgrade: required=%s installed=%s — spawning upgrade for %s",
            required,
            installed,
            project_name,
        )
        (spawn or _spawn_upgrade)(required, project_name)
        return True
    except Exception:
        log.exception("self-upgrade: skipped due to unexpected error")
    return False


def _open_log_fd(project_name: str):
    """Open a log file under :data:`_LOG_DIR`. Fall back to DEVNULL on failure.

    Reads the module-level :data:`_LOG_DIR` at call time so tests that
    monkeypatch it keep working; the shared helper takes the directory as an
    argument for exactly that reason.
    """
    return open_worker_log(_LOG_DIR, project_name)


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


def _run_install(required: str, *, lock_dir: Path | None = None) -> int:
    """Run the install command, record and log any failure, return rc.

    ``uv tool install --force`` removes before it verifies, so a non-zero rc can
    mean the tool venv is now half-removed and every entrypoint dangling. The
    record is what makes that visible: the log below reaches only this worker's
    own file, which nothing surfaces (#351).
    """
    cmd = _build_install_cmd(required)
    log.info("self-upgrade: running %s", " ".join(cmd))
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(
            "self-upgrade: install failed rc=%s stderr=%s",
            result.returncode,
            result.stderr,
        )
        if lock_dir is not None:
            record_self_upgrade_failure(
                lock_dir,
                required=required,
                installed=_installed_version(),
                rc=result.returncode,
                detail=(result.stderr or "").strip(),
            )
    elif lock_dir is not None:
        # Cleared only on a landing, so a debt nobody paid stays on the books.
        clear_self_upgrade_failure(lock_dir)
    return result.returncode


def _installed_version() -> str:
    """Best-effort: the version running right now, for the failure record."""
    try:
        return importlib_metadata.version("fraisier")
    except Exception:  # pragma: no cover - metadata is present in practice
        return "unknown"


def _unit_entrypoint(service: str) -> str | None:
    """The binary the installed unit names in ``ExecStart=``, or None.

    None means *unknown*, not *fine*: the unit may be absent, unreadable, or
    generated somewhere this worker cannot see. Callers treat that as abstention
    (see :func:`_entrypoint_is_broken`).
    """
    from fraisier.doctor import _exec_start_binary

    try:
        text = (_UNIT_DIR / service).read_text()
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        binary = _exec_start_binary(line)
        if binary is not None:
            return binary
    return None


def _entrypoint_is_broken(service: str) -> str | None:
    """The unresolvable entrypoint path, or None when it resolves or is unknown.

    ``uv tool install --force`` removes before it verifies, so after any install
    — successful or not — the binary this unit names may no longer exist. The
    worst thing to do at that moment is request a restart: the running process
    is the only working fraisier left on the host, and restarting it turns a
    latent problem into an outage.

    **Abstains when the unit cannot be read.** Blocking a restart on a guess
    would strand the host in the other direction, and the ``unit_entrypoints``
    doctor check reports the same condition without needing to be right here.
    """
    binary = _unit_entrypoint(service)
    if binary is None:
        return None
    target = Path(binary)
    # Existence first: os.access(X_OK) is permissive for root.
    if target.exists() and os.access(binary, os.X_OK):
        return None
    return binary


def _refuse_restart_for_broken_entrypoint(
    service: str,
    binary: str,
    *,
    required: str,
    lock_dir: Path | None,
    rc: int,
) -> None:
    """Log loudly and record that the venv did not survive the install."""
    log.error(
        "self-upgrade: NOT restarting %s — its ExecStart binary %s no longer "
        "resolves. `uv tool install --force` removes before it verifies, so the "
        "tool venv is half-removed and this unit would fail 203/EXEC. The "
        "process running now is the only working fraisier on this host. "
        "Recover with: sudo find ~/.local/share/uv/tools -name __pycache__ "
        "! -user $(id -un) -type d -exec rm -rf {} + && "
        "uv tool install --force fraisier==%s",
        service,
        binary,
        required,
    )
    if lock_dir is None:
        return
    consequence = (
        f"{service} ExecStart={binary} does not resolve after the install; "
        "restart refused to avoid 203/EXEC"
    )
    # A failed install already recorded uv's own stderr. That is the *cause* and
    # this is the *consequence*; an operator needs both, so the earlier detail is
    # carried forward rather than overwritten by this second write.
    previous = read_self_upgrade_failure(lock_dir)
    detail = (
        f"{previous.detail}\n\n{consequence}"
        if previous is not None and previous.detail
        else consequence
    )
    record_self_upgrade_failure(
        lock_dir,
        required=required,
        installed=_installed_version(),
        rc=rc,
        detail=detail,
    )


def _restart_outcome(socket_path: str, service: str) -> tuple[str, int]:
    """Request the restart and name the result, which used to be swallowed.

    ``_send_restart``'s rc was returned to a caller that discards it and never
    logged, so the last thing written was "requesting restart" whether or not
    the request landed.
    """
    rc = _send_restart(socket_path, service)
    return ("restart requested" if rc == 0 else "restart request failed", rc)


def _finish(outcome: str, rc: int, started_at: float) -> int:
    """Log a terminal outcome and hand back its rc.

    Every exit from :func:`_run_upgrade` goes through here. Before it existed
    the last line written was an *intention* — "requesting restart" — and the
    other exits said nothing about having ended at all, so an operator holding
    a 503 could not tell a running upgrade from a dead one. The elapsed time
    is the other half of that question: how long should I wait.
    """
    log.info(
        "self-upgrade: finished — %s (rc=%d) after %.1fs",
        outcome,
        rc,
        time.monotonic() - started_at,
    )
    return rc


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

    Entry and every exit are logged. These lines land in the worker log under
    :data:`_LOG_DIR`, not in the journal — that is correct and stays, since
    the worker log is the right place for install output. What changed is that
    the refusal message now names that directory, so the operator is told
    where to look.
    """
    started_at = time.monotonic()
    log.info(
        "self-upgrade: starting — install fraisier %s, then restart %s "
        "(this worker's output lands in %s)",
        required,
        service,
        _LOG_DIR,
    )
    if lock_dir is None:
        log.warning(
            "self-upgrade: lock_dir unresolved; running install + restart "
            "without drain coordination"
        )
        rc = _run_install(required, lock_dir=lock_dir)
        broken = _entrypoint_is_broken(service)
        if broken is not None:
            _refuse_restart_for_broken_entrypoint(
                service, broken, required=required, lock_dir=lock_dir, rc=rc
            )
            return _finish(
                "entrypoint broken; restart refused",
                rc or ENTRYPOINT_BROKEN_RC,
                started_at,
            )
        if rc != 0:
            return _finish("install failed", rc, started_at)
        if not socket_path:
            log.warning(
                "self-upgrade: FRAISIER_SYSTEMCTL_SOCKET not set; "
                "skipping restart of %s",
                service,
            )
            return _finish("no restart socket; install only", 0, started_at)
        return _finish(*_restart_outcome(socket_path, service), started_at)

    # Flag covers install + drain so dispatch refuses new deploys for the
    # entire upgrade window, not just the drain tail.
    with _with_draining_flag(lock_dir):
        rc = _run_install(required, lock_dir=lock_dir)
        # Checked on both outcomes: --force removes before it verifies, so a
        # non-zero rc and a zero rc can leave the same half-removed venv.
        broken = _entrypoint_is_broken(service)
        if broken is not None:
            _refuse_restart_for_broken_entrypoint(
                service, broken, required=required, lock_dir=lock_dir, rc=rc
            )
            return _finish(
                "entrypoint broken; restart refused",
                rc or ENTRYPOINT_BROKEN_RC,
                started_at,
            )
        if rc != 0:
            return _finish("install failed", rc, started_at)
        if not socket_path:
            log.warning(
                "self-upgrade: FRAISIER_SYSTEMCTL_SOCKET not set; "
                "skipping restart of %s",
                service,
            )
            return _finish("no restart socket; install only", 0, started_at)
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
            return _finish(
                "drain timeout; restart skipped", _DRAIN_TIMEOUT_RC, started_at
            )

    log.info("self-upgrade: deploys drained; requesting restart of %s", service)
    return _finish(*_restart_outcome(socket_path, service), started_at)


def _main() -> None:
    configure_worker_logging()
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

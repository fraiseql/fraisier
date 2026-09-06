"""Webhook handler for event-driven deployments.

Supports any Git provider: GitHub, GitLab, Gitea, Bitbucket, or custom.
"""

import asyncio
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ._env import get_int_env
from .config import get_config, reset_config
from .config._lazy_env import LazyEnv, to_str

if TYPE_CHECKING:
    from .config.loader import FraisierConfig
    from .database import FraisierDB
from .deferred_restart import maybe_apply_deferred_restarts
from .duration_estimate import build_estimate, to_dispatch_dict
from .errors import (
    ConfigurationError,
    DeploymentError,
    DeploymentLockError,
    FrameworkError,
)
from .git import GitProvider, WebhookEvent, get_provider
from .locking import (
    clear_draining_flag,
    deployment_lock,
    draining_flag_age_s,
    is_deployment_locked,
    is_draining,
)
from .refused_dispatch_record import (
    clear_refused_dispatch,
    record_refused_dispatch,
)
from .status import (
    FAILURE_STATES,
    DeploymentStatusFile,
    current_owner,
    read_status,
    reconcile_orphaned_deploys,
    write_status,
)
from .webhook_rate_limit import check_rate_limit
from .webhook_self_upgrade import maybe_self_upgrade
from .worker_logging import SELF_UPGRADE_LOG_DIR

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _validate_env_config(port: int, rate_limit: int) -> None:
    """Validate webhook server environment configuration."""
    if not 1 <= port <= 65535:
        msg = f"Invalid port: {port} — must be 1-65535"
        raise ValueError(msg)
    if rate_limit < 1:
        msg = f"Invalid rate limit: {rate_limit} — must be >= 1"
        raise ValueError(msg)


def _config_preflight() -> None:
    """Name an unloadable fraises.yaml in the journal, and start anyway (#383).

    A deploy that installed a config the loader refuses leaves the file at
    ``/opt``: the running webhook keeps its cached copy and looks fine, and the
    *next* restart — a self-upgrade, a reboot, a ``scaffold-install`` — used to
    die here. A webhook under ``Restart=on-failure`` that cannot start cannot
    be repaired by the redeploy the operator was told to run; one that starts
    and refuses per request can. Requests that need the configuration answer a
    structured error; ``fraisier doctor`` has a config check.
    """
    from fraisier.config import resolve_config_path

    try:
        get_config()
    except FileNotFoundError:
        return
    except FrameworkError as exc:
        try:
            path: Path | str = resolve_config_path()
        except FileNotFoundError:  # pragma: no cover - defensive
            path = "<unresolved>"
        logger.error(
            "%s cannot be loaded: %s. The webhook is starting anyway and will "
            "refuse every request that needs the configuration. Fix the file "
            "and restart, or redeploy; if the deploy that installed it is "
            "still rolling back, it puts the previous copy back itself.",
            path,
            exc,
        )


def _clear_stale_drain_flag() -> None:
    """Best-effort cleanup of a ``.draining`` flag left by a crashed worker."""
    try:
        lock_dir = _get_lock_dir(get_config())
    except FileNotFoundError:
        return
    except FrameworkError:
        # Already named by _config_preflight; a webhook that cannot read its
        # configuration must still start (#383).
        return
    if lock_dir is None:
        return
    try:
        clear_draining_flag(lock_dir)
    except OSError:
        logger.debug("Could not clear stale draining flag", exc_info=True)


def _reconcile_orphaned_deploys() -> None:
    """Close the record of any deploy whose process did not survive.

    Sits beside ``_clear_stale_drain_flag`` because it cleans up after the same
    kind of event, and runs at startup for a reason specific to #349: the thing
    that kills a deploy is a restart of this unit, so the restart that killed it
    is what brings this function up. Best-effort — a webhook that cannot tidy
    old records must still start and serve.
    """
    try:
        reconciled = reconcile_orphaned_deploys()
    except OSError:
        logger.debug("Could not reconcile orphaned deployment records", exc_info=True)
        return
    if reconciled:
        logger.warning(
            "Recorded %s as interrupted: the deploys that owned those records "
            "were terminated without reporting",
            ", ".join(reconciled),
        )


def _install_sighup_reload() -> asyncio.AbstractEventLoop | None:
    """Register a SIGHUP handler that forces a config reload, if possible.

    Makes ``systemctl reload`` (``ExecReload=/bin/kill -HUP $MAINPID``) drop
    the cached config so the next ``get_config()`` re-reads ``fraises.yaml``
    immediately (#278). Returns the loop the handler was bound to, so the
    caller can unregister it on shutdown, or ``None`` when registration is
    unavailable: no SIGHUP on this platform, or the loop is not on the main
    thread (Starlette's TestClient, some multi-worker setups). Registration
    must never crash startup — every failure degrades to "no handler", and
    SIGHUP then falls back to its default (terminate) disposition.
    """
    import signal

    if not hasattr(signal, "SIGHUP"):
        return None
    try:
        loop = asyncio.get_running_loop()

        def _reload() -> None:
            logger.info("SIGHUP received — reloading configuration")
            reset_config()

        loop.add_signal_handler(signal.SIGHUP, _reload)
    except (NotImplementedError, RuntimeError, ValueError):
        logger.debug(
            "SIGHUP config reload unavailable in this runtime; skipping",
            exc_info=True,
        )
        return None
    logger.info("SIGHUP config reload enabled (systemctl reload re-reads config)")
    return loop


def _remove_sighup_reload(loop: asyncio.AbstractEventLoop | None) -> None:
    """Best-effort removal of the SIGHUP handler installed at startup."""
    import signal

    if loop is None or not hasattr(signal, "SIGHUP"):
        return
    try:
        loop.remove_signal_handler(signal.SIGHUP)
    except (NotImplementedError, RuntimeError, ValueError):
        logger.debug("Could not remove SIGHUP handler", exc_info=True)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage webhook server lifecycle."""
    logger.info("Fraisier webhook server starting")
    _config_preflight()
    _clear_stale_drain_flag()
    _reconcile_orphaned_deploys()
    sighup_loop = _install_sighup_reload()
    yield
    _remove_sighup_reload(sighup_loop)
    logger.info("Fraisier webhook server shutting down")


app = FastAPI(
    title="Fraisier Webhook",
    description="Receives Git webhooks and triggers fraise deployments",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def structured_error_handler(
    _request: Request, exc: HTTPException
) -> JSONResponse:
    """Return structured JSON for all HTTP errors."""
    if isinstance(exc.detail, dict):
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


# Error type → recovery hint mapping
_RECOVERY_HINTS: dict[str, str] = {
    "authentication_error": "Check your webhook secret or deployment token.",
    "validation_error": "Check the request payload and provider configuration.",
    "configuration_error": (
        "The host's fraises.yaml or its environment is incomplete; "
        "the journal names what is missing."
    ),
    "not_found": "Verify the fraise name and that a status file exists.",
    "service_unavailable": (
        "Self-upgrade in progress. Retry after the indicated delay."
    ),
}


_RETRY_AFTER_DEFAULT_S = 60

#: How long a ``.draining`` flag is believed. Six times the 600s drain timeout,
#: plus room for the install — generous on purpose, because past it the flag is
#: *ignored* and a deploy is allowed to start (#365).
_FLAG_MAX_AGE_DEFAULT_S = 3600


def _self_upgrade_flag_max_age_s() -> float:
    """Single source of truth for how long a ``.draining`` flag counts.

    Reads ``webhook.self_upgrade_flag_max_age_s``. Falls back to the default
    on any read failure, matching :func:`_retry_after_seconds` — a host that
    cannot read its own config must still make the call the default makes,
    not raise inside a request.
    """
    try:
        return float(
            get_config().webhook.get(
                "self_upgrade_flag_max_age_s", _FLAG_MAX_AGE_DEFAULT_S
            )
        )
    except (FileNotFoundError, AttributeError, ValueError, TypeError):
        return _FLAG_MAX_AGE_DEFAULT_S


def _retry_after_seconds() -> int:
    """Single source of truth for the draining ``Retry-After`` value.

    Returned by both the HTTP header and each per-fraise ``retry_after_s``
    field so the two cannot drift. Reads ``webhook.self_upgrade_retry_after_s``
    from config; falls back to the default on any read failure.
    """
    try:
        return int(
            get_config().webhook.get(
                "self_upgrade_retry_after_s", _RETRY_AFTER_DEFAULT_S
            )
        )
    except (FileNotFoundError, AttributeError, ValueError, TypeError):
        return _RETRY_AFTER_DEFAULT_S


def _structured_error(
    status_code: int,
    error_type: str,
    message: str,
) -> HTTPException:
    """Create an HTTPException with structured JSON detail."""
    return HTTPException(
        status_code=status_code,
        detail={
            "error_type": error_type,
            "message": message,
            "recovery_hint": _RECOVERY_HINTS.get(error_type, ""),
        },
    )


def _unavailable(
    retry_after_s: int,
    deployments: list[dict[str, Any]],
    *,
    branch: str | None = None,
    provider: str | None = None,
    webhook_id: int | None = None,
) -> JSONResponse:
    """Build a 503 JSONResponse with a ``Retry-After`` header.

    Mirrors the ``_structured_error`` JSON shape and folds the dispatch
    ``deployments`` list in so callers can correlate which fraises were
    refused.
    """
    body: dict[str, Any] = {
        "error_type": "service_unavailable",
        "message": "Webhook is draining for self-upgrade.",
        "recovery_hint": _RECOVERY_HINTS["service_unavailable"],
        "deployments": deployments,
    }
    if branch is not None:
        body["branch"] = branch
    if provider is not None:
        body["provider"] = provider
    if webhook_id is not None:
        body["webhook_id"] = webhook_id
    return JSONResponse(
        status_code=503,
        content=body,
        headers={"Retry-After": str(retry_after_s)},
    )


def _record_refusals(
    lock_dir: Path,
    event: WebhookEvent,
    fraise_configs: list[dict[str, Any]],
    webhook_id: int,
) -> None:
    """Leave a record of every target this refusal dropped (#365).

    Best-effort, and wrapped rather than trusted: ``record_refused_dispatch``
    already swallows ``OSError`` on the write, but this sits in front of the
    503 the webhook still has to send, and nothing here may be able to stop
    it.
    """
    try:
        for fc in fraise_configs:
            record_refused_dispatch(
                lock_dir,
                fraise=fc["fraise_name"],
                environment=fc["environment"],
                branch=event.branch or "",
                commit_sha=event.commit_sha or "",
                webhook_id=webhook_id,
            )
    except Exception:
        logger.warning("could not record the refused dispatch", exc_info=True)


def _discharge_refusal(
    config: "FraisierConfig", fraise_name: str, environment: str
) -> None:
    """Clear this target's refused-dispatch entry after a deploy that landed.

    Only a success calls this. A deploy that ran and failed is a different
    fact, recorded elsewhere, and does not settle "a request was dropped".

    Best-effort, like its neighbours in that branch: it runs inside a
    ``BackgroundTask`` where nothing may raise.
    """
    lock_dir = _get_lock_dir(config)
    if lock_dir is None:
        return
    try:
        clear_refused_dispatch(lock_dir, fraise=fraise_name, environment=environment)
    except Exception:
        logger.warning(
            "could not clear the refused dispatch for %s/%s",
            fraise_name,
            environment,
            exc_info=True,
        )


def _is_draining_response(payload: dict[str, Any]) -> bool:
    """Return True iff ``payload`` was built by ``_draining_response``.

    The single integration point where the plain-dict dispatch result is
    elevated to HTTP 503. Any new caller of ``_dispatch_deployment`` that
    bypasses ``generic_webhook`` must call this predicate too.
    """
    if payload.get("status") == "draining":
        return True
    return any(
        isinstance(d, dict) and d.get("status") == "draining"
        for d in payload.get("deployments", [])
    )


def _resolve_provider_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Materialize every ``LazyEnv`` value in a provider-config mapping.

    Git provider config carries secrets (``webhook_secret``, API
    ``token``, app key) that are commonly ``!envvar``-tagged in
    fraises.yaml. Provider constructors expect ``str`` for these
    fields and call ``.encode()`` on them; a raw ``LazyEnv`` would
    raise ``AttributeError`` deep inside ``hmac.new``. This helper
    resolves every ``LazyEnv`` once at the consumer boundary.
    Non-LazyEnv values (``bool`` / ``int`` / ``None`` / nested dicts)
    pass through untouched.
    """
    return {k: (to_str(v) if isinstance(v, LazyEnv) else v) for k, v in raw.items()}


def get_git_provider() -> GitProvider:
    """Get configured Git provider from environment or config."""
    config = get_config()
    git_config = config.get_git_provider_config()

    provider_name = os.getenv("FRAISIER_GIT_PROVIDER") or git_config.get(
        "provider", "github"
    )

    provider_config = _resolve_provider_config(
        {
            "webhook_secret": os.getenv("FRAISIER_WEBHOOK_SECRET"),
            "base_url": os.getenv("FRAISIER_GIT_URL"),
            **git_config.get(provider_name, {}),
        }
    )

    return get_provider(provider_name, provider_config)


async def execute_deployment(
    fraise_name: str,
    environment: str,
    fraise_config: dict[str, Any],
    webhook_id: int | None = None,
    git_branch: str | None = None,
    git_commit: str | None = None,
) -> None:
    """Execute deployment in background.

    Args:
        fraise_name: Fraise name (e.g., "my_api")
        environment: Environment (e.g., "production")
        fraise_config: Fraise configuration from fraises.yaml
        webhook_id: ID of webhook event that triggered this
        git_branch: Git branch being deployed
        git_commit: Git commit SHA being deployed
    """
    from .database import get_db

    db = get_db()

    # Skip if this commit is already deployed (version gating)
    if git_commit:
        current = read_status(fraise_name)
        if current and current.commit_sha == git_commit:
            logger.info(
                f"Commit {git_commit[:7]} already deployed for "
                f"{fraise_name}/{environment}, skipping"
            )
            return

    logger.info(f"Starting deployment: {fraise_name} -> {environment}")

    # Acquire deployment lock (file or database backend per config)
    try:
        with deployment_lock(fraise_name):
            await _run_deployment(
                fraise_name,
                environment,
                fraise_config,
                webhook_id,
                git_branch,
                git_commit,
                db,
            )
    except DeploymentLockError:
        logger.warning(
            f"Deploy already running for {fraise_name}/{environment}, skipping"
        )


async def _run_deployment(
    fraise_name: str,
    environment: str,
    fraise_config: dict[str, Any],
    webhook_id: int | None,
    git_branch: str | None,
    git_commit: str | None,
    db: "FraisierDB",
) -> None:
    """Run the actual deployment within a lock."""
    upgrading = False
    try:
        fraise_type = fraise_config.get("type")

        # Inject identity + git info so deployer records correctly
        config = get_config()
        deploy_user = config.get_deploy_user(fraise_name, environment)
        deploy_config = {
            **fraise_config,
            "fraise_name": fraise_name,
            "environment": environment,
            "branch": git_branch or fraise_config.get("branch", "main"),
            "git_commit": git_commit,
            "deploy_user": deploy_user,
        }

        # Get deployer from the shared registry — always with a local runner:
        # the webhook process is already running on the target host, so the
        # ssh: block (intended for client-side CLI commands) must not apply.
        #
        # The registry is what the daemon uses too. The if-chain that used to
        # be here knew only api/etl/docker_compose, so a push mapped to a
        # `scheduled` or `backup` fraise was answered `deployment_triggered`
        # and then dropped right here, with nothing recorded (#379).
        from .deployers.registry import UnknownFraiseTypeError, build_deployer
        from .runners import LocalRunner

        try:
            deployer = build_deployer(fraise_type, deploy_config, runner=LocalRunner())
        except UnknownFraiseTypeError as exc:
            # No deployer to write through, so the record is written directly:
            # answering "triggered" and leaving no trace is what this fixes.
            logger.error("Cannot deploy %s/%s: %s", fraise_name, environment, exc)
            write_status(
                DeploymentStatusFile(
                    fraise_name=fraise_name,
                    environment=environment,
                    state="failed",
                    finished_at=datetime.now().isoformat(),
                    error_message=str(exc),
                    **current_owner(),
                )
            )
            return

        # Execute deployment (deployer handles DB recording internally)
        result = deployer.execute()

        # Link webhook event to the deployment recorded by the deployer
        # The deployer records via _start_db_record, get latest deployment
        if webhook_id:
            deployments = db.get_recent_deployments(
                limit=1, fraise=fraise_name, environment=environment
            )
            if deployments:
                db.link_webhook_to_deployment(
                    webhook_id, deployments[0]["pk_deployment"]
                )

        if result.success:
            # Update fraise state
            db.update_fraise_state(
                fraise=fraise_name,
                environment=environment,
                version=result.new_version or "unknown",
                status="healthy",
                deployed_by="webhook",
            )
            logger.info(
                f"Deployment successful: {fraise_name}/{environment} "
                f"({result.old_version} -> {result.new_version})"
            )
            _discharge_refusal(config, fraise_name, environment)
            app_path = fraise_config.get("app_path")
            if app_path:
                webhook_cfg = config.webhook
                upgrading = maybe_self_upgrade(
                    Path(app_path),
                    project_name=config.project_name,
                    enabled=bool(webhook_cfg.get("self_upgrade", True)),
                )
        else:
            logger.error(
                f"Deployment failed: {fraise_name}/{environment} "
                f"- {result.error_message}"
            )

        # Pay whatever install.sh deferred because this deploy was in flight
        # (#349). Run on failure too: the units were installed either way, and a
        # rollback that restores the previous fraises.yaml reinstalls their old
        # bytes, so install.sh records no debt and this is a no-op.
        #
        # Skipped while a self-upgrade is in flight. That worker restarts the
        # webhook anyway, which is the debt — and both workers raise the same
        # single `.draining` flag, so the first to finish would clear it out
        # from under the other.
        if not upgrading:
            lock_dir = _get_lock_dir(config)
            if lock_dir is not None:
                maybe_apply_deferred_restarts(
                    lock_dir=lock_dir,
                    socket_path=os.environ.get("FRAISIER_SYSTEMCTL_SOCKET", ""),
                )

    except (DeploymentError, ConfigurationError, OSError) as e:
        logger.exception(
            "Deployment error for %s/%s [%s]: %s",
            fraise_name,
            environment,
            type(e).__name__,
            e,
        )
    except Exception as e:
        logger.exception(
            "Unexpected deployment error for %s/%s [%s]: %s",
            fraise_name,
            environment,
            type(e).__name__,
            e,
        )


def _get_lock_dir(config: "FraisierConfig") -> Path | None:
    """Extract lock directory from config, or None when it is unusable.

    A relative ``deployment.lock_dir`` is a misconfiguration: the lock file
    would land wherever the process happens to be, and no two processes would
    agree on where it is. The webhook treats it as unresolvable — the same as
    an absent one — and carries on with the checks it can still make. The
    deploy itself fails loudly when it tries to take the lock, which is the
    moment the answer actually matters.
    """
    try:
        lock_dir = Path(config.deployment.lock_dir)
    except (AttributeError, FileNotFoundError):
        return None
    if not lock_dir.is_absolute():
        logger.error(
            "deployment.lock_dir is %r, which is not an absolute path. "
            "Deploy-concurrency and drain checks are being skipped; the next "
            "deploy will refuse to start until this is fixed.",
            str(lock_dir),
        )
        return None
    return lock_dir


def _build_estimate(
    fraise_config: dict[str, Any], fraise_name: str, environment: str
) -> dict[str, Any] | None:
    """Return ``{estimated_duration_s, estimated_ready_at, estimate_confidence}``
    or None if the fraise has no database section or the lookup fails."""
    try:
        from .database import get_db

        result = build_estimate(get_db(), fraise_config, fraise_name, environment)
    except Exception:
        logger.exception(
            "build_estimate: failed to build estimate for %s/%s",
            fraise_name,
            environment,
        )
        return None
    if result is None:
        return None
    return to_dispatch_dict(result)


def _draining_response(
    event: WebhookEvent,
    fraise_configs: list[dict[str, Any]],
    webhook_id: int,
    *,
    flag_age_s: float | None = None,
) -> dict[str, Any]:
    """Build the dispatch response when the host is draining for self-upgrade.

    Callers downstream of ``_dispatch_deployment`` (currently only
    ``generic_webhook`` via ``_is_draining_response``) elevate this to HTTP
    503 + ``Retry-After``. The per-fraise ``retry_after_s`` and the HTTP
    header both read from :func:`_retry_after_seconds`.

    ``flag_age_s`` rides along as ``draining_age_s`` so a caller that logs the
    body can tell a healthy upgrade from a stuck one. It is the *only* thing
    this body gained: the response goes to an unauthenticated caller, so no
    path, version or host detail belongs in it. Omitted when the age is
    unknown rather than reported as zero.
    """
    retry_after = _retry_after_seconds()
    deployments = [
        {
            "status": "draining",
            "reason": "self-upgrade in progress",
            "fraise": fc["fraise_name"],
            "environment": fc["environment"],
            "retry_after_s": retry_after,
            **({} if flag_age_s is None else {"draining_age_s": int(flag_age_s)}),
        }
        for fc in fraise_configs
    ]
    if len(deployments) == 1:
        d = deployments[0]
        return {
            **d,
            "branch": event.branch,
            "provider": event.provider,
            "webhook_id": webhook_id,
        }
    return {
        "status": "deployments_triggered",
        "deployments": deployments,
        "branch": event.branch,
        "provider": event.provider,
        "webhook_id": webhook_id,
    }


def _dispatch_deployment(
    event: WebhookEvent,
    background_tasks: BackgroundTasks,
    webhook_id: int,
    config: "FraisierConfig",
) -> dict[str, Any]:
    """Find matching fraises for a push event and trigger deployments.

    Returns a plain dict. The endpoint (``generic_webhook``) inspects the
    result via ``_is_draining_response`` and converts a draining shape to
    HTTP 503 + ``Retry-After`` — bypassing ``generic_webhook`` would miss
    that elevation, so any future caller must call the predicate too.
    """
    assert event.branch is not None  # caller guards on event.branch before dispatch
    fraise_configs = config.get_fraises_for_branch(event.branch)

    if not fraise_configs:
        logger.info(f"No fraise configured for branch: {event.branch}")
        return {
            "status": "ignored",
            "reason": f"No fraise configured for branch '{event.branch}'",
            "provider": event.provider,
            "webhook_id": webhook_id,
        }

    lock_dir = _get_lock_dir(config)
    if lock_dir is not None and is_draining(
        lock_dir, max_age_s=_self_upgrade_flag_max_age_s()
    ):
        flag_age_s = draining_flag_age_s(lock_dir)
        logger.warning(
            "Self-upgrade in progress (flag raised %s ago); refusing dispatch "
            "for branch %s. Everything after the spawn runs in a detached "
            "worker whose output goes to %s, not to this journal.",
            "an unknown time" if flag_age_s is None else f"{flag_age_s:.0f}s",
            event.branch,
            SELF_UPGRADE_LOG_DIR,
        )
        # After the log line, not before: recording touches the filesystem and
        # the journal should carry the refusal even if that write is what hangs.
        _record_refusals(lock_dir, event, fraise_configs, webhook_id)
        return _draining_response(
            event, fraise_configs, webhook_id, flag_age_s=flag_age_s
        )

    deployments: list[dict[str, Any]] = []

    for fraise_config in fraise_configs:
        fraise_name = fraise_config["fraise_name"]
        environment = fraise_config["environment"]

        if is_deployment_locked(fraise_name, lock_dir=lock_dir):
            logger.info(
                "Deploy already running for %s/%s, skipping",
                fraise_name,
                environment,
            )
            deployments.append(
                {
                    "status": "skipped",
                    "reason": "deployment already running",
                    "fraise": fraise_name,
                    "environment": environment,
                }
            )
            continue

        logger.info(f"Triggering deployment: {fraise_name} -> {environment}")
        background_tasks.add_task(
            execute_deployment,
            fraise_name=fraise_name,
            environment=environment,
            fraise_config=fraise_config,
            webhook_id=webhook_id,
            git_branch=event.branch,
            git_commit=event.commit_sha,
        )
        deployment: dict[str, Any] = {
            "status": "deployment_triggered",
            "fraise": fraise_name,
            "environment": environment,
        }
        estimate = _build_estimate(fraise_config, fraise_name, environment)
        if estimate is not None:
            deployment.update(estimate)
        deployments.append(deployment)

    # Single-fraise backward compatibility: return flat response
    if len(fraise_configs) == 1:
        d = deployments[0]
        return {
            **d,
            "branch": event.branch,
            "provider": event.provider,
            "webhook_id": webhook_id,
        }

    return {
        "status": "deployments_triggered",
        "deployments": deployments,
        "branch": event.branch,
        "provider": event.provider,
        "webhook_id": webhook_id,
    }


def process_webhook_event(
    event: WebhookEvent,
    background_tasks: BackgroundTasks,
    webhook_id: int,
) -> dict[str, Any]:
    """Process a normalized webhook event."""
    if event.is_ping:
        return {
            "status": "pong",
            "message": "Webhook configured successfully",
            "provider": event.provider,
            "webhook_id": webhook_id,
        }

    if event.is_push and event.branch:
        try:
            config = get_config()
        except FileNotFoundError:
            return {
                "status": "ignored",
                "reason": "No configuration file found",
                "provider": event.provider,
                "webhook_id": webhook_id,
            }
        logger.info(f"Push to branch: {event.branch} (provider: {event.provider})")
        return _dispatch_deployment(event, background_tasks, webhook_id, config)

    return {
        "status": "ignored",
        "event": event.event_type,
        "provider": event.provider,
        "webhook_id": webhook_id,
    }


def _detect_git_provider(headers: dict[str, str], query_provider: str | None) -> str:
    """Auto-detect git provider from headers or query parameter."""
    if query_provider:
        return query_provider

    header_signatures: dict[str, str] = {
        "x-github-event": "github",
        "x-gitlab-event": "gitlab",
        "x-gitea-event": "gitea",
        "x-event-key": "bitbucket",
    }
    lower_headers = {k.lower() for k in headers}
    for header, provider in header_signatures.items():
        if header in lower_headers:
            return provider

    try:
        config = get_config()
        return config.get_git_provider_config().get("provider", "github")
    except FileNotFoundError:
        return "github"


def _collect_webhook_secrets() -> list[str]:
    """Collect all configured webhook secrets from environment and config.

    Sources (in order):
    1. FRAISIER_WEBHOOK_SECRET (base env var, backwards compatible)
    2. FRAISIER_WEBHOOK_SECRET_* (per-environment env vars)
    3. Webhook secrets from fraises.yaml git provider config (fallback)

    Secrets shorter than 32 characters are skipped with a warning.
    Duplicates are removed.
    """
    _MIN_LEN = 32
    seen: set[str] = set()
    secrets: list[str] = []

    def _add(secret: str, source: str) -> None:
        if len(secret) < _MIN_LEN:
            logger.warning(
                "%s is too short (%d chars, minimum %d) — skipping",
                source,
                len(secret),
                _MIN_LEN,
            )
            return
        if secret not in seen:
            seen.add(secret)
            secrets.append(secret)

    # 1. Base env var
    base = os.getenv("FRAISIER_WEBHOOK_SECRET")
    if base:
        _add(base, "FRAISIER_WEBHOOK_SECRET")

    # 2. Per-environment env vars (FRAISIER_WEBHOOK_SECRET_*)
    prefix = "FRAISIER_WEBHOOK_SECRET_"
    for key, value in sorted(os.environ.items()):
        if key.startswith(prefix) and value:
            _add(value, key)

    return secrets


def _verify_signature(
    provider_name: str, body: bytes, headers: dict[str, str]
) -> tuple[GitProvider, dict[str, str]]:
    """Build provider, verify signature, return provider + headers.

    Tries each configured webhook secret until one validates the signature.
    """
    try:
        git_config = get_config().get_git_provider_config()
    except FileNotFoundError:
        git_config = {}
    except FrameworkError as e:
        # An unloadable fraises.yaml (#383). The host has one and it is broken,
        # which is not the same as not having one: proceeding on env-var
        # secrets alone would dispatch against a configuration nobody can
        # read. Refuse, and put the reason in the journal rather than in an
        # unauthenticated response.
        logger.error("Cannot read the git provider configuration: %s", e)
        raise _structured_error(
            500,
            "configuration_error",
            "this host's fraises.yaml cannot be loaded — see the journal",
        ) from e

    normalized_headers = {k.lower(): v for k, v in headers.items()}
    secrets = _collect_webhook_secrets()

    # When no explicit secrets are configured, still attempt with None so that
    # the provider's verify_webhook_signature (which returns False for missing
    # secret) produces a proper 401 rather than silently skipping verification.
    candidates = secrets or [None]

    for secret in candidates:
        try:
            provider_config = _resolve_provider_config(
                {
                    "webhook_secret": secret,
                    **git_config.get(provider_name, {}),
                }
            )
            provider = get_provider(provider_name, provider_config)
        except ValueError as e:
            raise _structured_error(400, "validation_error", str(e)) from e
        except ConfigurationError as e:
            # An unset `!envvar` secret. `LazyEnv.resolve` raises a
            # FrameworkError, not a ValueError, so this used to fall out of the
            # handler and FastAPI answered a bare 500 with an empty body on
            # every delivery — fail-closed, but with the variable's name only
            # in a traceback (#381). The message names the variable and the
            # YAML path; it goes to the journal, not to an unauthenticated
            # caller.
            logger.error("Webhook secret is not resolvable: %s", e)
            raise _structured_error(
                500,
                "configuration_error",
                "webhook secret is not configured on this host — see the journal",
            ) from e

        if provider.verify_webhook_signature(body, normalized_headers):
            return provider, normalized_headers

    logger.warning(
        "Invalid webhook signature from %s",
        provider_name,
    )
    raise _structured_error(401, "authentication_error", "Invalid webhook signature")


async def _normalize_event(
    provider: GitProvider,
    request: Request,
    normalized_headers: dict[str, str],
) -> WebhookEvent:
    """Parse request JSON and build a normalized WebhookEvent."""
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise _structured_error(400, "validation_error", "Invalid JSON payload") from e

    event = provider.parse_webhook_event(normalized_headers, payload)
    logger.info(f"Received {event.provider} event: {event.event_type}")
    return event


@app.post("/webhook", response_model=None)
async def generic_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any] | JSONResponse:
    """Receive webhook from any Git provider.

    The provider is auto-detected from headers, or can be specified
    via query parameter: /webhook?provider=gitlab

    Returns ``JSONResponse(503)`` with a ``Retry-After`` header when the
    webhook is draining for a self-upgrade (see ``_is_draining_response``).
    """
    from .database import get_db

    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip):
        raise _structured_error(429, "rate_limited", "Too many requests")

    body = await request.body()
    headers = dict(request.headers)

    provider_name = _detect_git_provider(headers, request.query_params.get("provider"))
    provider, normalized_headers = _verify_signature(provider_name, body, headers)

    if provider_name == "github":
        delivery_id = normalized_headers.get("x-github-delivery", "").strip()
        if not delivery_id:
            raise _structured_error(400, "validation_error", "missing delivery id")
        from .git.github import _delivery_dedupe

        if _delivery_dedupe.seen(delivery_id):
            logger.info("Replay rejected: delivery %s already processed", delivery_id)
            raise _structured_error(409, "replay_rejected", "replay rejected")

    event = await _normalize_event(provider, request, normalized_headers)

    db = get_db()
    webhook_id = db.record_webhook_event(
        event_type=event.event_type,
        payload=json.dumps(await request.json()),
        branch=event.branch,
        commit_sha=event.commit_sha,
        sender=event.sender,
        git_provider=event.provider,
    )

    result = process_webhook_event(event, background_tasks, webhook_id)
    if _is_draining_response(result):
        deployments = result.get("deployments") or [
            {k: v for k, v in result.items() if k != "webhook_id"}
        ]
        return _unavailable(
            _retry_after_seconds(),
            deployments,
            branch=result.get("branch"),
            provider=result.get("provider"),
            webhook_id=result.get("webhook_id"),
        )
    return result


def _get_webhook_secret() -> str:
    """Get the first webhook secret for backwards-compatible single-secret callers.

    Raises RuntimeError if no secret is configured or if the secret
    is shorter than 32 characters.
    """
    secrets = _collect_webhook_secrets()
    if not secrets:
        # Check if there's a raw env var that was too short
        raw = os.getenv("FRAISIER_WEBHOOK_SECRET")
        if raw and len(raw) < 32:
            msg = (
                "FRAISIER_WEBHOOK_SECRET must be at least 32 characters. "
                f"Current length: {len(raw)}"
            )
            raise RuntimeError(msg)
        msg = (
            "FRAISIER_WEBHOOK_SECRET must be set. "
            "Generate one with: python -c "
            '"import secrets; print(secrets.token_urlsafe(48))"'
        )
        raise RuntimeError(msg)
    return secrets[0]


@app.get("/api/status/{fraise_name}")
async def get_deploy_status(fraise_name: str) -> dict[str, Any]:
    """Public deployment status — safe fields only."""
    import re

    if not re.match(r"^[a-zA-Z0-9_\-]+$", fraise_name):
        raise _structured_error(
            400, "validation_error", f"Invalid fraise name: {fraise_name!r}"
        )
    status = read_status(fraise_name)
    if status is None:
        raise _structured_error(404, "not_found", f"Fraise '{fraise_name}' not found")
    return {
        "state": status.state,
        "version": status.version,
        "commit_sha": status.commit_sha,
        "environment": status.environment,
    }


@app.get("/api/status/{fraise_name}/details")
async def get_deploy_details(fraise_name: str, request: Request) -> dict[str, Any]:
    """Authenticated deployment details — includes error info."""
    import re

    if not re.match(r"^[a-zA-Z0-9_\-]+$", fraise_name):
        raise _structured_error(
            400, "validation_error", f"Invalid fraise name: {fraise_name!r}"
        )
    token = request.headers.get("X-Deployment-Token")
    secrets = _collect_webhook_secrets()
    if not token or not any(hmac.compare_digest(token, s) for s in secrets):
        raise _structured_error(403, "authentication_error", "Invalid or missing token")

    status = read_status(fraise_name)
    if status is None:
        raise _structured_error(404, "not_found", f"Fraise '{fraise_name}' not found")

    # Membership, not equality: `rollback_failed` means the schema may be dirty,
    # and answering "No failure to report" on the endpoint an operator queries
    # for failure detail is the worst possible reply (#293).
    if status.state not in FAILURE_STATES:
        return {
            "state": status.state,
            "version": status.version,
            "commit_sha": status.commit_sha,
            "environment": status.environment,
            "message": "No failure to report",
        }

    return {
        "state": status.state,
        "version": status.version,
        "commit_sha": status.commit_sha,
        "environment": status.environment,
        "error_message": status.error_message,
        "migration_report": status.migration_report,
        "last_error": status.last_error,
        "started_at": status.started_at,
        "finished_at": status.finished_at,
    }


# Legacy endpoint for backward compatibility
@app.post("/webhook/github", response_model=None)
async def github_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any] | JSONResponse:
    """GitHub-specific webhook endpoint (legacy, use /webhook instead)."""
    from starlette.datastructures import QueryParams

    # Inject the provider hint via a proper QueryParams object instead of
    # mutating _query_params as a raw dict (wrong type, breaks on API changes).
    request._query_params = QueryParams("provider=github")
    return await generic_webhook(request, background_tasks)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy", "service": "fraisier-webhook"}


@app.get("/fraises")
async def list_fraises() -> dict[str, Any]:
    """List all configured fraises."""
    config = get_config()
    return {
        "fraises": config.list_fraises_detailed(),
        "branch_mapping": config.branch_mapping,
    }


@app.get("/providers")
async def list_providers() -> dict[str, Any]:
    """List supported Git providers."""
    from .git import list_providers

    try:
        configured = get_config().get_git_provider_config().get("provider", "github")
    except FileNotFoundError:
        configured = "github"
    return {
        "providers": list_providers(),
        "configured": configured,
    }


def run_server() -> None:
    """Run the webhook server."""
    import uvicorn

    host = os.getenv("FRAISIER_HOST", "0.0.0.0")
    port = get_int_env("FRAISIER_PORT", default=8080, min_value=1)

    rate_limit = get_int_env("FRAISIER_RATE_LIMIT", default=10, min_value=1)
    _validate_env_config(port, rate_limit)

    logger.info(f"Starting Fraisier webhook server on {host}:{port}")

    config = uvicorn.Config(
        "fraisier.webhook:app",
        host=host,
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    run_server()

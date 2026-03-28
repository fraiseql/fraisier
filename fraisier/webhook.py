"""Webhook handler for event-driven deployments.

Supports any Git provider: GitHub, GitLab, Gitea, Bitbucket, or custom.
"""

import hmac
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from ._env import get_int_env
from .config import get_config
from .errors import ConfigurationError, DeploymentError, DeploymentLockError
from .git import GitProvider, WebhookEvent, get_provider
from .locking import deployment_lock, is_deployment_locked
from .status import read_status
from .webhook_rate_limit import check_rate_limit

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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage webhook server lifecycle."""
    logger.info("Fraisier webhook server starting")
    yield
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
    "not_found": "Verify the fraise name and that a status file exists.",
}


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


def get_git_provider() -> GitProvider:
    """Get configured Git provider from environment or config."""
    config = get_config()
    git_config = config.get_git_provider_config()

    provider_name = os.getenv("FRAISIER_GIT_PROVIDER") or git_config.get(
        "provider", "github"
    )

    provider_config = {
        "webhook_secret": os.getenv("FRAISIER_WEBHOOK_SECRET"),
        "base_url": os.getenv("FRAISIER_GIT_URL"),
        **git_config.get(provider_name, {}),
    }

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
    db: Any,
) -> None:
    """Run the actual deployment within a lock."""
    try:
        fraise_type = fraise_config.get("type")

        # Inject identity + git info so deployer records correctly
        deploy_config = {
            **fraise_config,
            "fraise_name": fraise_name,
            "environment": environment,
            "branch": git_branch or fraise_config.get("branch", "main"),
            "git_commit": git_commit,
        }

        # Get deployer
        from .runners import runner_from_config

        runner = runner_from_config(deploy_config.get("ssh"))

        if fraise_type == "api":
            from .deployers.api import APIDeployer

            deployer = APIDeployer(deploy_config, runner=runner)
        elif fraise_type == "etl":
            from .deployers.etl import ETLDeployer

            deployer = ETLDeployer(deploy_config, runner=runner)
        elif fraise_type == "docker_compose":
            from .deployers.docker_compose import DockerComposeDeployer

            deployer = DockerComposeDeployer(deploy_config, runner=runner)
        else:
            logger.error(f"Unknown fraise type: {fraise_type}")
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
        else:
            logger.error(
                f"Deployment failed: {fraise_name}/{environment} "
                f"- {result.error_message}"
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


def _get_lock_dir(config: Any) -> Path | None:
    """Extract lock directory from config."""
    try:
        return Path(config.deployment.lock_dir)
    except (AttributeError, FileNotFoundError):
        return None


def _dispatch_deployment(
    event: WebhookEvent,
    background_tasks: BackgroundTasks,
    webhook_id: int,
    config: Any,
) -> dict[str, Any]:
    """Find matching fraises for a push event and trigger deployments."""
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
        deployments.append(
            {
                "status": "deployment_triggered",
                "fraise": fraise_name,
                "environment": environment,
            }
        )

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


def _verify_signature(
    provider_name: str, body: bytes, headers: dict[str, str]
) -> tuple[GitProvider, dict[str, str]]:
    """Build provider, verify signature, return provider + headers."""
    try:
        try:
            git_config = get_config().get_git_provider_config()
        except FileNotFoundError:
            git_config = {}
        provider_config = {
            "webhook_secret": os.getenv("FRAISIER_WEBHOOK_SECRET"),
            **git_config.get(provider_name, {}),
        }
        provider = get_provider(provider_name, provider_config)
    except ValueError as e:
        raise _structured_error(400, "validation_error", str(e)) from e

    normalized_headers = {k.lower(): v for k, v in headers.items()}

    if not provider.verify_webhook_signature(body, normalized_headers):
        logger.warning(f"Invalid webhook signature from {provider_name}")
        raise _structured_error(
            401, "authentication_error", "Invalid webhook signature"
        )

    return provider, normalized_headers


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


@app.post("/webhook")
async def generic_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Receive webhook from any Git provider.

    The provider is auto-detected from headers, or can be specified
    via query parameter: /webhook?provider=gitlab
    """
    from .database import get_db

    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip):
        raise _structured_error(429, "rate_limited", "Too many requests")

    body = await request.body()
    headers = dict(request.headers)

    provider_name = _detect_git_provider(headers, request.query_params.get("provider"))
    provider, normalized_headers = _verify_signature(provider_name, body, headers)
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

    return process_webhook_event(event, background_tasks, webhook_id)


def _get_webhook_secret() -> str:
    """Get webhook secret from config for token authentication.

    Raises RuntimeError if no secret is configured or if the secret
    is shorter than 32 characters.
    """
    secret = os.getenv("FRAISIER_WEBHOOK_SECRET")
    if not secret:
        # Walk provider configs to find a webhook_secret
        try:
            config = get_config()
            git_config = config.get_git_provider_config()
            for provider_conf in git_config.values():
                is_dict = isinstance(provider_conf, dict)
                if is_dict and "webhook_secret" in provider_conf:
                    secret = provider_conf["webhook_secret"]
                    break
        except FileNotFoundError:
            pass
    if not secret:
        msg = (
            "FRAISIER_WEBHOOK_SECRET must be set. "
            "Generate one with: python -c "
            '"import secrets; print(secrets.token_urlsafe(48))"'
        )
        raise RuntimeError(msg)
    if len(secret) < 32:
        msg = (
            "FRAISIER_WEBHOOK_SECRET must be at least 32 characters. "
            f"Current length: {len(secret)}"
        )
        raise RuntimeError(msg)
    return secret


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
    expected = _get_webhook_secret()
    if not token or not hmac.compare_digest(token, expected):
        raise _structured_error(403, "authentication_error", "Invalid or missing token")

    status = read_status(fraise_name)
    if status is None:
        raise _structured_error(404, "not_found", f"Fraise '{fraise_name}' not found")

    if status.state != "failed":
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
@app.post("/webhook/github")
async def github_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """GitHub-specific webhook endpoint (legacy, use /webhook instead)."""
    # Add provider hint to query params
    request._query_params = request.query_params._dict.copy()
    request._query_params["provider"] = "github"
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

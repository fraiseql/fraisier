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

from .config import get_config
from .errors import DeploymentLockError
from .git import GitProvider, WebhookEvent, get_provider
from .locking import file_deployment_lock
from .status import read_status

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


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
    git_config = config._config.get("git", {})

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

    # Acquire file lock to prevent concurrent deployments
    try:
        lock_dir = None
        try:
            deployment_raw = get_config()._config.get("deployment", {}) or {}
            if deployment_raw.get("lock_dir"):
                lock_dir = Path(deployment_raw["lock_dir"])
        except FileNotFoundError:
            pass

        with file_deployment_lock(fraise_name, lock_dir=lock_dir):
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
    deployment_id = None

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
        if fraise_type == "api":
            from .deployers.api import APIDeployer

            deployer = APIDeployer(deploy_config)
        elif fraise_type == "etl":
            from .deployers.etl import ETLDeployer

            deployer = ETLDeployer(deploy_config)
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

    except Exception as e:
        logger.exception(f"Deployment error for {fraise_name}/{environment}: {e}")
        if deployment_id:
            db.complete_deployment(
                deployment_id=deployment_id,
                success=False,
                error_message=str(e),
            )


def process_webhook_event(
    event: WebhookEvent,
    background_tasks: BackgroundTasks,
    webhook_id: int,
) -> dict[str, Any]:
    """Process a normalized webhook event.

    Args:
        event: Normalized webhook event
        background_tasks: FastAPI background tasks
        webhook_id: Database ID of recorded event

    Returns:
        Response dict
    """
    # Handle ping events first (no config needed)
    if event.is_ping:
        return {
            "status": "pong",
            "message": "Webhook configured successfully",
            "provider": event.provider,
            "webhook_id": webhook_id,
        }

    # Handle push events
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

        # Get fraise for this branch
        fraise_config = config.get_fraise_for_branch(event.branch)

        if fraise_config:
            fraise_name = fraise_config["fraise_name"]
            environment = fraise_config["environment"]
            logger.info(f"Triggering deployment: {fraise_name} -> {environment}")

            # Execute deployment in background
            background_tasks.add_task(
                execute_deployment,
                fraise_name=fraise_name,
                environment=environment,
                fraise_config=fraise_config,
                webhook_id=webhook_id,
                git_branch=event.branch,
                git_commit=event.commit_sha,
            )

            return {
                "status": "deployment_triggered",
                "fraise": fraise_name,
                "environment": environment,
                "branch": event.branch,
                "provider": event.provider,
                "webhook_id": webhook_id,
            }
        else:
            logger.info(f"No fraise configured for branch: {event.branch}")
            return {
                "status": "ignored",
                "reason": f"No fraise configured for branch '{event.branch}'",
                "provider": event.provider,
                "webhook_id": webhook_id,
            }

    # Ignore other events
    return {
        "status": "ignored",
        "event": event.event_type,
        "provider": event.provider,
        "webhook_id": webhook_id,
    }


@app.post("/webhook")
async def generic_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Receive webhook from any Git provider.

    The provider is auto-detected from headers, or can be specified
    via query parameter: /webhook?provider=gitlab

    Returns:
        Status of the webhook processing
    """
    from .database import get_db

    # Get raw body for signature verification
    body = await request.body()
    headers = dict(request.headers)

    # Auto-detect provider from headers or use configured default
    provider_name = request.query_params.get("provider")

    if not provider_name:
        # Try to auto-detect from headers
        if "X-GitHub-Event" in headers or "x-github-event" in headers:
            provider_name = "github"
        elif "X-Gitlab-Event" in headers or "x-gitlab-event" in headers:
            provider_name = "gitlab"
        elif "X-Gitea-Event" in headers or "x-gitea-event" in headers:
            provider_name = "gitea"
        elif "X-Event-Key" in headers or "x-event-key" in headers:
            provider_name = "bitbucket"
        else:
            # Fall back to configured default
            try:
                config = get_config()
                git_config = config._config.get("git", {})
                provider_name = git_config.get("provider", "github")
            except FileNotFoundError:
                provider_name = "github"

    # Get provider and verify signature
    try:
        try:
            git_config = get_config()._config.get("git", {})
        except FileNotFoundError:
            git_config = {}
        provider_config = {
            "webhook_secret": os.getenv("FRAISIER_WEBHOOK_SECRET"),
            **git_config.get(provider_name, {}),
        }
        provider = get_provider(provider_name, provider_config)
    except ValueError as e:
        raise _structured_error(400, "validation_error", str(e)) from e

    # Normalize headers to handle case variations
    normalized_headers = {k.title(): v for k, v in headers.items()}

    # Verify signature
    if not provider.verify_webhook_signature(body, normalized_headers):
        logger.warning(f"Invalid webhook signature from {provider_name}")
        raise _structured_error(
            401, "authentication_error", "Invalid webhook signature"
        )

    # Parse payload
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise _structured_error(400, "validation_error", "Invalid JSON payload") from e

    # Parse event
    event = provider.parse_webhook_event(normalized_headers, payload)
    logger.info(f"Received {event.provider} event: {event.event_type}")

    # Record webhook event in database
    db = get_db()
    webhook_id = db.record_webhook_event(
        event_type=event.event_type,
        payload=json.dumps(payload),
        branch=event.branch,
        commit_sha=event.commit_sha,
        sender=event.sender,
        git_provider=event.provider,
    )

    # Process the event
    return process_webhook_event(event, background_tasks, webhook_id)


def _get_webhook_secret() -> str:
    """Get webhook secret from config for token authentication."""
    config = get_config()
    git_config = config.get_git_provider_config()
    # Check environment variable first, then provider-specific config
    secret = os.getenv("FRAISIER_WEBHOOK_SECRET")
    if secret:
        return secret
    # Walk provider configs to find a webhook_secret
    for provider_conf in git_config.values():
        if isinstance(provider_conf, dict) and "webhook_secret" in provider_conf:
            return provider_conf["webhook_secret"]
    return ""


@app.get("/api/status/{fraise_name}")
async def get_deploy_status(fraise_name: str) -> dict[str, Any]:
    """Public deployment status — safe fields only."""
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
        configured = get_config()._config.get("git", {}).get("provider", "github")
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
    port = int(os.getenv("FRAISIER_PORT", "8080"))

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

"""Integration tests for the webhook → deployment chain.

Uses FastAPI TestClient to send real HTTP requests to the webhook endpoint.
Tests valid/invalid signatures, branch routing, and database recording.
"""

import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from fraisier.config import FraisierConfig
from fraisier.git import WebhookEvent
from fraisier.webhook import app

WEBHOOK_SECRET = "test-webhook-secret-for-integration-tests"

_PUSH_HEADERS = {
    "Content-Type": "application/json",
    "X-GitHub-Event": "push",
}
_PING_HEADERS = {
    "Content-Type": "application/json",
    "X-GitHub-Event": "ping",
}


def _github_signature(body: bytes, secret: str) -> str:
    """Compute GitHub-style HMAC-SHA256 signature."""
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _mock_github_provider(*, verify: bool = True):
    """Create a mock GitHub provider."""
    provider = MagicMock()
    provider.verify_webhook_signature.return_value = verify
    provider.name = "github"

    def parse_event(headers, payload):
        ref = payload.get("ref", "")
        branch = (
            ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None
        )
        event_type = headers.get("x-github-event", "unknown")
        commits = payload.get("commits", [])
        commit_sha = commits[0].get("id") if commits else None
        return WebhookEvent(
            provider="github",
            event_type=event_type,
            branch=branch,
            commit_sha=commit_sha,
            sender=payload.get("pusher", {}).get("name"),
            is_push=(event_type == "push"),
            is_ping=(event_type == "ping"),
        )

    provider.parse_webhook_event.side_effect = parse_event
    return provider


def _webhook_config(tmp_path: Path) -> FraisierConfig:
    """Config with branch_mapping for webhook routing."""
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(f"""
git:
  provider: github
  github:
    webhook_secret: "{WEBHOOK_SECRET}"

branch_mapping:
  main:
    fraise: my_api
    environment: production

fraises:
  my_api:
    type: api
    description: Test API
    environments:
      production:
        app_path: /tmp/test-api
        systemd_service: test-api.service
        health_check:
          url: http://localhost:8000/health
          timeout: 5
""")
    return FraisierConfig(str(config_file))


def _post_webhook(config, provider, body, headers):
    """POST to /webhook with mocked config and provider."""
    with (
        patch("fraisier.webhook.get_config", return_value=config),
        patch(
            "fraisier.webhook.get_provider",
            return_value=provider,
        ),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        return client.post("/webhook", content=body, headers=headers)


class TestWebhookSignatureValidation:
    """Webhook must validate signatures via the provider."""

    def test_valid_signature_returns_200(self, tmp_path, test_db):
        """POST /webhook with valid signature succeeds."""
        config = _webhook_config(tmp_path)
        payload = {
            "ref": "refs/heads/main",
            "pusher": {"name": "dev"},
            "commits": [{"id": "abc123"}],
        }
        body = json.dumps(payload).encode()
        provider = _mock_github_provider(verify=True)

        response = _post_webhook(config, provider, body, _PUSH_HEADERS)
        assert response.status_code == 200

    def test_invalid_signature_returns_401(self, tmp_path, test_db):
        """POST /webhook with invalid signature returns 401."""
        config = _webhook_config(tmp_path)
        payload = {"ref": "refs/heads/main", "pusher": {"name": "dev"}}
        body = json.dumps(payload).encode()
        provider = _mock_github_provider(verify=False)

        response = _post_webhook(config, provider, body, _PUSH_HEADERS)
        assert response.status_code == 401
        data = response.json()
        assert data["error_type"] == "authentication_error"


class TestWebhookBranchRouting:
    """Webhook routes pushes based on branch_mapping config."""

    def test_configured_branch_triggers_deployment(self, tmp_path, test_db):
        """Push to 'main' (mapped) triggers deployment."""
        config = _webhook_config(tmp_path)
        payload = {
            "ref": "refs/heads/main",
            "repository": {"name": "test-repo"},
            "pusher": {"name": "dev"},
            "commits": [{"id": "abc123def456"}],
        }
        body = json.dumps(payload).encode()

        response = _post_webhook(config, _mock_github_provider(), body, _PUSH_HEADERS)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "deployment_triggered"
        assert data["fraise"] == "my_api"
        assert data["environment"] == "production"

    def test_unconfigured_branch_returns_ignored(self, tmp_path, test_db):
        """Push to unmapped branch returns 200 with 'ignored'."""
        config = _webhook_config(tmp_path)
        payload = {
            "ref": "refs/heads/feature/untracked",
            "pusher": {"name": "dev"},
            "commits": [{"id": "abc123"}],
        }
        body = json.dumps(payload).encode()

        response = _post_webhook(config, _mock_github_provider(), body, _PUSH_HEADERS)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ignored"
        assert "feature/untracked" in data.get("reason", "")

    def test_ping_event_returns_pong(self, tmp_path, test_db):
        """GitHub ping event returns pong."""
        config = _webhook_config(tmp_path)
        payload = {"zen": "Keep it logically awesome."}
        body = json.dumps(payload).encode()

        response = _post_webhook(config, _mock_github_provider(), body, _PING_HEADERS)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "pong"


class TestWebhookDatabaseRecording:
    """Webhook events are recorded in the database."""

    def test_push_event_recorded(self, tmp_path, test_db):
        """A valid push creates a webhook event record."""
        config = _webhook_config(tmp_path)
        payload = {
            "ref": "refs/heads/main",
            "pusher": {"name": "dev"},
            "commits": [{"id": "abc123def456"}],
        }
        body = json.dumps(payload).encode()

        response = _post_webhook(config, _mock_github_provider(), body, _PUSH_HEADERS)

        assert response.status_code == 200
        data = response.json()
        assert "webhook_id" in data

        webhooks = test_db.get_recent_webhooks(limit=1)
        assert len(webhooks) == 1
        assert webhooks[0]["event_type"] == "push"
        assert webhooks[0]["branch_name"] == "main"
        assert webhooks[0]["commit_sha"] == "abc123def456"

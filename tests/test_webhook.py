"""Tests for webhook handler and FastAPI routes."""

import json
import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from fraisier.git import WebhookEvent
from fraisier.status import DeploymentStatusFile
from fraisier.webhook import (
    app,
    execute_deployment,
    process_webhook_event,
)


@pytest.fixture
def webhook_client():
    """Create test client for FastAPI app."""
    return TestClient(app)


@pytest.fixture
def sample_webhook_payload():
    """Sample GitHub webhook payload."""
    return {
        "ref": "refs/heads/main",
        "repository": {
            "name": "test-repo",
            "url": "https://github.com/test/test-repo",
        },
        "pusher": {
            "name": "developer",
            "email": "dev@example.com",
        },
        "commits": [
            {
                "id": "abc123def456",
                "message": "Deploy to production",
                "timestamp": "2026-01-22T10:30:00Z",
            }
        ],
    }


@pytest.fixture
def sample_webhook_event():
    """Sample normalized webhook event."""
    return WebhookEvent(
        provider="github",
        event_type="push",
        branch="main",
        commit_sha="abc123def456",
        sender="developer",
        is_push=True,
        is_ping=False,
    )


class TestExecuteDeployment:
    """Tests for execute_deployment background task."""

    @pytest.fixture(autouse=True)
    def _mock_lock(self, tmp_path):
        """Auto-redirect deployment lock to tmp_path for all tests in this class."""

        @contextmanager
        def tmp_lock(fraise_name):
            from fraisier.locking import file_deployment_lock as real_lock

            with real_lock(fraise_name, lock_dir=tmp_path) as path:
                yield path

        with patch("fraisier.webhook.deployment_lock", side_effect=tmp_lock):
            yield

    @pytest.mark.asyncio
    async def test_execute_deployment_api_success(self, test_db, mock_subprocess):
        """Test successful API deployment via webhook."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="Deployment successful\n",
        )

        fraise_config = {
            "type": "api",
            "app_path": "/tmp/test-api",
            "systemd_service": "test-api.service",
            "health_check": {"url": "http://localhost:8000/health", "timeout": 10},
        }

        with patch("fraisier.webhook.get_config") as mock_config:
            mock_config.return_value._config = {"git": {}}

            await execute_deployment(
                fraise_name="my_api",
                environment="production",
                fraise_config=fraise_config,
                webhook_id=1,
                git_branch="main",
                git_commit="abc123",
            )

            # Verify deployment was recorded
            deployments = test_db.get_recent_deployments(limit=1)
            assert len(deployments) > 0
            assert deployments[0]["fraise_name"] == "my_api"
            assert deployments[0]["environment"] == "production"

    @pytest.mark.asyncio
    async def test_execute_deployment_with_webhook_link(self, test_db):
        """Test that webhook ID is linked to deployment."""
        fraise_config = {
            "type": "api",
            "app_path": "/tmp/test-api",
            "systemd_service": "test-api.service",
            "health_check": {"url": "http://localhost:8000/health", "timeout": 10},
        }

        # Record a webhook event first
        webhook_id = test_db.record_webhook_event(
            event_type="push",
            payload=json.dumps({"test": "payload"}),
            branch="main",
            commit_sha="abc123",
            sender="dev",
            git_provider="github",
        )

        with patch("fraisier.webhook.get_config") as mock_config:
            mock_config.return_value._config = {"git": {}}

            await execute_deployment(
                fraise_name="my_api",
                environment="production",
                fraise_config=fraise_config,
                webhook_id=webhook_id,
                git_branch="main",
                git_commit="abc123",
            )

            # Verify webhook was linked to deployment
            webhooks = test_db.get_recent_webhooks(limit=1)
            assert len(webhooks) > 0
            assert webhooks[0]["processed"] == 1
            assert webhooks[0]["fk_deployment"] is not None

    @pytest.mark.asyncio
    async def test_execute_deployment_etl_type(self, test_db, mock_subprocess):
        """Test ETL deployment via webhook."""
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="ETL ran\n")

        fraise_config = {
            "type": "etl",
            "app_path": "/var/etl",
            "script_path": "scripts/pipeline.py",
        }

        with patch("fraisier.webhook.get_config") as mock_config:
            mock_config.return_value._config = {"git": {}}

            await execute_deployment(
                fraise_name="data_pipeline",
                environment="production",
                fraise_config=fraise_config,
                git_branch="main",
            )

            deployments = test_db.get_recent_deployments(limit=1)
            assert len(deployments) > 0
            assert deployments[0]["fraise_name"] == "data_pipeline"

    @pytest.mark.asyncio
    async def test_skips_when_commit_sha_already_deployed(self, test_db, caplog):
        """Duplicate commit SHA is skipped — no deployment executed."""
        caplog.set_level("INFO", logger="fraisier.webhook")
        current_status = DeploymentStatusFile(
            fraise_name="my_api",
            environment="production",
            state="success",
            version="1.0.0",
            commit_sha="abc123",
        )

        fraise_config = {
            "type": "api",
            "app_path": "/tmp/test-api",
            "systemd_service": "test-api.service",
        }

        with patch("fraisier.webhook.read_status", return_value=current_status):
            await execute_deployment(
                fraise_name="my_api",
                environment="production",
                fraise_config=fraise_config,
                git_commit="abc123",
            )

        assert "already deployed" in caplog.text
        # No deployment should have been recorded
        deployments = test_db.get_recent_deployments(limit=1, fraise="my_api")
        assert len(deployments) == 0

    @pytest.mark.asyncio
    async def test_proceeds_when_commit_sha_differs(self, test_db, mock_subprocess):
        """New commit SHA proceeds with deployment."""
        current_status = DeploymentStatusFile(
            fraise_name="my_api",
            environment="production",
            state="success",
            version="1.0.0",
            commit_sha="old_sha_111",
        )
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="Deployment successful\n",
        )

        fraise_config = {
            "type": "api",
            "app_path": "/tmp/test-api",
            "systemd_service": "test-api.service",
            "health_check": {"url": "http://localhost:8000/health", "timeout": 10},
        }

        with (
            patch("fraisier.webhook.read_status", return_value=current_status),
            patch("fraisier.webhook.get_config") as mock_config,
        ):
            mock_config.return_value._config = {"git": {}}

            await execute_deployment(
                fraise_name="my_api",
                environment="production",
                fraise_config=fraise_config,
                git_commit="new_sha_222",
                git_branch="main",
            )

        # Deployment should have proceeded
        deployments = test_db.get_recent_deployments(limit=1, fraise="my_api")
        assert len(deployments) > 0

    @pytest.mark.asyncio
    async def test_proceeds_when_no_prior_status(self, test_db, mock_subprocess):
        """First deployment (no status file) proceeds normally."""
        mock_subprocess.return_value = MagicMock(
            returncode=0,
            stdout="Deployment successful\n",
        )

        fraise_config = {
            "type": "api",
            "app_path": "/tmp/test-api",
            "systemd_service": "test-api.service",
            "health_check": {"url": "http://localhost:8000/health", "timeout": 10},
        }

        with (
            patch("fraisier.webhook.read_status", return_value=None),
            patch("fraisier.webhook.get_config") as mock_config,
        ):
            mock_config.return_value._config = {"git": {}}

            await execute_deployment(
                fraise_name="my_api",
                environment="production",
                fraise_config=fraise_config,
                git_commit="first_sha",
                git_branch="main",
            )

        deployments = test_db.get_recent_deployments(limit=1, fraise="my_api")
        assert len(deployments) > 0

    @pytest.mark.asyncio
    async def test_execute_deployment_unknown_type_logs_error(self, test_db, caplog):
        """Test that unknown fraise type is logged."""
        fraise_config = {"type": "unknown"}

        with patch("fraisier.webhook.get_config") as mock_config:
            mock_config.return_value._config = {"git": {}}

            await execute_deployment(
                fraise_name="unknown_fraise",
                environment="production",
                fraise_config=fraise_config,
            )

            # Should log error about unknown type
            assert "Unknown fraise type" in caplog.text

    @pytest.mark.asyncio
    async def test_acquires_lock_during_deployment(
        self, test_db, mock_subprocess, tmp_path
    ):
        """Deployment acquires deployment_lock for the fraise."""
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="Deployment successful\n"
        )

        fraise_config = {
            "type": "api",
            "app_path": "/tmp/test-api",
            "systemd_service": "test-api.service",
            "health_check": {"url": "http://localhost:8000/health", "timeout": 10},
        }

        lock_acquired = []

        from fraisier.locking import file_deployment_lock as real_lock

        @contextmanager
        def tracking_lock(fraise_name):
            with real_lock(fraise_name, lock_dir=tmp_path) as path:
                lock_acquired.append(fraise_name)
                yield path

        with (
            patch("fraisier.webhook.read_status", return_value=None),
            patch("fraisier.webhook.get_config") as mock_config,
            patch(
                "fraisier.webhook.deployment_lock",
                side_effect=tracking_lock,
            ),
        ):
            mock_config.return_value._config = {"git": {}}

            await execute_deployment(
                fraise_name="my_api",
                environment="production",
                fraise_config=fraise_config,
                git_commit="new_sha",
                git_branch="main",
            )

        assert "my_api" in lock_acquired

    @pytest.mark.asyncio
    async def test_concurrent_deploy_rejected_by_lock(self, test_db, caplog):
        """Second deployment to same fraise is rejected when lock is held."""
        caplog.set_level("WARNING", logger="fraisier.webhook")
        fraise_config = {
            "type": "api",
            "app_path": "/tmp/test-api",
            "systemd_service": "test-api.service",
        }

        from fraisier.errors import DeploymentLockError

        with (
            patch("fraisier.webhook.read_status", return_value=None),
            patch(
                "fraisier.webhook.deployment_lock",
                side_effect=DeploymentLockError("Deploy already running for my_api"),
            ),
        ):
            await execute_deployment(
                fraise_name="my_api",
                environment="production",
                fraise_config=fraise_config,
                git_commit="sha_123",
            )

        assert "already running" in caplog.text.lower() or "lock" in caplog.text.lower()
        # No deployment should have been recorded
        deployments = test_db.get_recent_deployments(limit=1, fraise="my_api")
        assert len(deployments) == 0


class TestRunDeploymentErrorRecording:
    """Exception handlers in _run_deployment log errors properly."""

    @pytest.fixture(autouse=True)
    def _mock_lock(self, tmp_path):
        @contextmanager
        def tmp_lock(fraise_name):
            from fraisier.locking import file_deployment_lock as real_lock

            with real_lock(fraise_name, lock_dir=tmp_path) as path:
                yield path

        with patch("fraisier.webhook.deployment_lock", side_effect=tmp_lock):
            yield

    @pytest.mark.asyncio
    async def test_deployment_error_is_logged(self, test_db, caplog):
        """DeploymentError during _run_deployment is logged with context."""
        from fraisier.errors import DeploymentError

        caplog.set_level("ERROR")
        fraise_config = {
            "type": "api",
            "app_path": "/tmp/test-api",
            "systemd_service": "test-api.service",
        }

        mock_deployer = MagicMock()
        mock_deployer.execute.side_effect = DeploymentError("migration failed")

        with (
            patch("fraisier.webhook.get_config") as mock_config,
            patch("fraisier.runners.runner_from_config"),
            patch(
                "fraisier.deployers.api.APIDeployer",
                return_value=mock_deployer,
            ),
        ):
            mock_config.return_value._config = {"git": {}}

            await execute_deployment(
                fraise_name="my_api",
                environment="production",
                fraise_config=fraise_config,
                git_branch="main",
            )

        assert "migration failed" in caplog.text
        assert "my_api" in caplog.text

    @pytest.mark.asyncio
    async def test_unexpected_error_is_logged(self, test_db, caplog):
        """Unexpected exceptions during _run_deployment are logged."""
        caplog.set_level("ERROR")
        fraise_config = {
            "type": "api",
            "app_path": "/tmp/test-api",
            "systemd_service": "test-api.service",
        }

        mock_deployer = MagicMock()
        mock_deployer.execute.side_effect = RuntimeError("segfault")

        with (
            patch("fraisier.webhook.get_config") as mock_config,
            patch("fraisier.runners.runner_from_config"),
            patch(
                "fraisier.deployers.api.APIDeployer",
                return_value=mock_deployer,
            ),
        ):
            mock_config.return_value._config = {"git": {}}

            await execute_deployment(
                fraise_name="my_api",
                environment="production",
                fraise_config=fraise_config,
                git_branch="main",
            )

        assert "segfault" in caplog.text
        assert "Unexpected" in caplog.text


class TestProcessWebhookEvent:
    """Tests for process_webhook_event function."""

    def test_process_push_event_with_configured_fraise(self, test_db):
        """Test push event triggers deployment for configured fraise."""
        event = WebhookEvent(
            provider="github",
            event_type="push",
            branch="main",
            commit_sha="abc123",
            sender="dev",
            is_push=True,
            is_ping=False,
        )

        with patch("fraisier.webhook.get_config") as mock_config:
            mock_config_obj = MagicMock()
            mock_config_obj.get_fraises_for_branch.return_value = [{
                "fraise_name": "my_api",
                "environment": "production",
                "type": "api",
                "app_path": "/tmp/api",
            }]
            mock_config.return_value = mock_config_obj

            from fastapi import BackgroundTasks

            background_tasks = BackgroundTasks()

            result = process_webhook_event(event, background_tasks, webhook_id=1)

            assert result["status"] == "deployment_triggered"
            assert result["fraise"] == "my_api"
            assert result["environment"] == "production"
            assert result["branch"] == "main"
            assert result["provider"] == "github"

    def test_process_push_returns_skipped_when_deploy_locked(self, test_db):
        """Webhook returns 'skipped' when a deploy is already running."""
        event = WebhookEvent(
            provider="github",
            event_type="push",
            branch="main",
            commit_sha="abc123",
            sender="dev",
            is_push=True,
            is_ping=False,
        )

        with (
            patch("fraisier.webhook.get_config") as mock_config,
            patch("fraisier.webhook.is_deployment_locked", return_value=True),
        ):
            mock_config_obj = MagicMock()
            mock_config_obj.get_fraises_for_branch.return_value = [{
                "fraise_name": "my_api",
                "environment": "production",
                "type": "api",
                "app_path": "/tmp/api",
            }]
            mock_config_obj._config = {"deployment": {}}
            mock_config.return_value = mock_config_obj

            from fastapi import BackgroundTasks

            background_tasks = BackgroundTasks()

            result = process_webhook_event(event, background_tasks, webhook_id=1)

            assert result["status"] == "skipped"
            assert result["reason"] == "deployment already running"
            assert result["fraise"] == "my_api"

    def test_process_push_event_no_configured_fraise(self):
        """Test push event with no configured fraise."""
        event = WebhookEvent(
            provider="gitlab",
            event_type="push",
            branch="feature/xyz",
            commit_sha="xyz789",
            sender="dev",
            is_push=True,
            is_ping=False,
        )

        with patch("fraisier.webhook.get_config") as mock_config:
            mock_config_obj = MagicMock()
            mock_config_obj.get_fraises_for_branch.return_value = []
            mock_config.return_value = mock_config_obj

            from fastapi import BackgroundTasks

            background_tasks = BackgroundTasks()

            result = process_webhook_event(event, background_tasks, webhook_id=1)

            assert result["status"] == "ignored"
            assert "No fraise configured" in result["reason"]
            assert result["provider"] == "gitlab"

    def test_process_ping_event(self):
        """Test ping event returns pong."""
        event = WebhookEvent(
            provider="github",
            event_type="ping",
            branch=None,
            commit_sha=None,
            sender=None,
            is_push=False,
            is_ping=True,
        )

        from fastapi import BackgroundTasks

        background_tasks = BackgroundTasks()

        result = process_webhook_event(event, background_tasks, webhook_id=1)

        assert result["status"] == "pong"
        assert result["provider"] == "github"
        assert "Webhook configured successfully" in result["message"]

    def test_process_other_events_ignored(self):
        """Test that PR events are ignored."""
        event = WebhookEvent(
            provider="github",
            event_type="pull_request",
            branch="feature/new",
            commit_sha="def456",
            sender="dev",
            is_push=False,
            is_ping=False,
        )

        from fastapi import BackgroundTasks

        background_tasks = BackgroundTasks()

        result = process_webhook_event(event, background_tasks, webhook_id=1)

        assert result["status"] == "ignored"
        assert result["event"] == "pull_request"


class TestMultiFraiseDispatch:
    """Tests for multi-fraise branch mapping dispatch."""

    def test_multi_fraise_branch_creates_multiple_tasks(self, test_db):
        """Push to branch with 3 mapped fraises creates 3 background tasks."""
        event = WebhookEvent(
            provider="github",
            event_type="push",
            branch="main",
            commit_sha="abc123",
            sender="dev",
            is_push=True,
        )

        configs = [
            {"fraise_name": "api_a", "environment": "prod", "type": "api"},
            {"fraise_name": "api_b", "environment": "prod", "type": "api"},
            {"fraise_name": "worker", "environment": "prod", "type": "etl"},
        ]

        with patch("fraisier.webhook.get_config") as mock_config:
            mock_config_obj = MagicMock()
            mock_config_obj.get_fraises_for_branch.return_value = configs
            mock_config.return_value = mock_config_obj

            from fastapi import BackgroundTasks

            background_tasks = MagicMock(spec=BackgroundTasks)

            result = process_webhook_event(event, background_tasks, webhook_id=1)

        assert result["status"] == "deployments_triggered"
        assert len(result["deployments"]) == 3
        assert background_tasks.add_task.call_count == 3

    def test_single_fraise_branch_still_works(self, test_db):
        """Push to branch with 1 mapped fraise creates 1 background task."""
        event = WebhookEvent(
            provider="github",
            event_type="push",
            branch="main",
            commit_sha="abc123",
            sender="dev",
            is_push=True,
        )

        with patch("fraisier.webhook.get_config") as mock_config:
            mock_config_obj = MagicMock()
            mock_config_obj.get_fraises_for_branch.return_value = [
                {"fraise_name": "my_api", "environment": "prod", "type": "api"},
            ]
            mock_config.return_value = mock_config_obj

            from fastapi import BackgroundTasks

            background_tasks = MagicMock(spec=BackgroundTasks)

            result = process_webhook_event(event, background_tasks, webhook_id=1)

        assert result["status"] == "deployment_triggered"
        assert background_tasks.add_task.call_count == 1

    def test_locked_fraise_skipped_others_still_deploy(self, test_db):
        """If one fraise is locked, others still deploy."""
        event = WebhookEvent(
            provider="github",
            event_type="push",
            branch="main",
            commit_sha="abc123",
            sender="dev",
            is_push=True,
        )

        configs = [
            {"fraise_name": "api_a", "environment": "prod", "type": "api"},
            {"fraise_name": "api_b", "environment": "prod", "type": "api"},
        ]

        def locked_check(name, lock_dir=None):
            return name == "api_a"

        with (
            patch("fraisier.webhook.get_config") as mock_config,
            patch("fraisier.webhook.is_deployment_locked", side_effect=locked_check),
        ):
            mock_config_obj = MagicMock()
            mock_config_obj.get_fraises_for_branch.return_value = configs
            mock_config.return_value = mock_config_obj

            from fastapi import BackgroundTasks

            background_tasks = MagicMock(spec=BackgroundTasks)

            result = process_webhook_event(event, background_tasks, webhook_id=1)

        # api_b should deploy, api_a should be skipped
        assert background_tasks.add_task.call_count == 1
        assert len(result["deployments"]) == 2


class TestWebhookRoutes:
    """Tests for FastAPI webhook routes."""

    def test_health_check_endpoint(self, webhook_client):
        """Test /health endpoint."""
        response = webhook_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "fraisier-webhook"

    def test_list_providers_endpoint(self, webhook_client):
        """Test /providers endpoint."""
        response = webhook_client.get("/providers")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        assert "github" in data["providers"]
        assert "gitlab" in data["providers"]
        assert "gitea" in data["providers"]
        assert "bitbucket" in data["providers"]
        assert "configured" in data

    def test_list_fraises_endpoint(self, webhook_client, sample_config):
        """Test /fraises endpoint."""
        with patch("fraisier.webhook.get_config") as mock_config:
            mock_config.return_value = sample_config

            response = webhook_client.get("/fraises")
            assert response.status_code == 200
            data = response.json()
            assert "fraises" in data
            assert len(data["fraises"]) > 0

    def test_webhook_post_invalid_signature(
        self, webhook_client, sample_webhook_payload
    ):
        """Test webhook with invalid signature is rejected."""
        response = webhook_client.post(
            "/webhook",
            json=sample_webhook_payload,
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": "sha256=invalid_signature",
            },
        )
        assert response.status_code == 401
        data = response.json()
        assert data["error_type"] == "authentication_error"

    def test_webhook_post_malformed_json(self, webhook_client):
        """Test webhook with malformed JSON is rejected."""
        with patch("fraisier.webhook.get_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.verify_webhook_signature.return_value = True
            mock_get_provider.return_value = mock_provider

            response = webhook_client.post(
                "/webhook",
                content=b"not json",
                headers={"X-GitHub-Event": "push"},
            )
        assert response.status_code == 400
        data = response.json()
        assert data["error_type"] == "validation_error"

    def test_webhook_post_unknown_provider(
        self, webhook_client, sample_webhook_payload
    ):
        """Test webhook with unknown provider."""
        response = webhook_client.post(
            "/webhook?provider=unknown",
            json=sample_webhook_payload,
        )
        assert response.status_code == 400

    def test_webhook_provider_auto_detection_github(self, webhook_client, test_db):
        """Test webhook provider auto-detection for GitHub."""
        with patch("fraisier.webhook.get_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.verify_webhook_signature.return_value = True
            mock_provider.parse_webhook_event.return_value = WebhookEvent(
                provider="github",
                event_type="ping",
                branch=None,
                commit_sha=None,
                sender=None,
                is_push=False,
                is_ping=True,
            )
            mock_get_provider.return_value = mock_provider

            response = webhook_client.post(
                "/webhook",
                json={"zen": "test"},
                headers={"X-GitHub-Event": "ping"},
            )

            assert response.status_code == 200
            # Verify provider was detected
            mock_get_provider.assert_called()
            call_args = mock_get_provider.call_args
            assert call_args[0][0] == "github"

    def test_webhook_provider_auto_detection_gitlab(self, webhook_client, test_db):
        """Test webhook provider auto-detection for GitLab."""
        with patch("fraisier.webhook.get_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.verify_webhook_signature.return_value = True
            mock_provider.parse_webhook_event.return_value = WebhookEvent(
                provider="gitlab",
                event_type="ping",
                branch=None,
                commit_sha=None,
                sender=None,
                is_push=False,
                is_ping=True,
            )
            mock_get_provider.return_value = mock_provider

            response = webhook_client.post(
                "/webhook",
                json={"hook": "test"},
                headers={"X-Gitlab-Event": "ping"},
            )

            assert response.status_code == 200
            # Verify provider was detected
            mock_get_provider.assert_called()
            call_args = mock_get_provider.call_args
            assert call_args[0][0] == "gitlab"

    def test_webhook_records_event_in_database(self, webhook_client, test_db):
        """Test webhook event is recorded in database."""
        with patch("fraisier.webhook.get_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.verify_webhook_signature.return_value = True
            mock_provider.parse_webhook_event.return_value = WebhookEvent(
                provider="github",
                event_type="push",
                branch="main",
                commit_sha="abc123",
                sender="developer",
                is_push=True,
                is_ping=False,
            )
            mock_get_provider.return_value = mock_provider

            payload = {"test": "data"}
            response = webhook_client.post(
                "/webhook",
                json=payload,
                headers={"X-GitHub-Event": "push"},
            )

            assert response.status_code == 200

            # Verify event was recorded
            webhooks = test_db.get_recent_webhooks(limit=1)
            assert len(webhooks) > 0
            assert webhooks[0]["event_type"] == "push"
            assert webhooks[0]["branch_name"] == "main"
            assert webhooks[0]["commit_sha"] == "abc123"
            assert webhooks[0]["sender"] == "developer"
            assert webhooks[0]["git_provider"] == "github"

    def test_github_legacy_endpoint(self, webhook_client, test_db):
        """Test legacy /webhook/github endpoint still works."""
        with patch("fraisier.webhook.get_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.verify_webhook_signature.return_value = True
            mock_provider.parse_webhook_event.return_value = WebhookEvent(
                provider="github",
                event_type="ping",
                branch=None,
                commit_sha=None,
                sender=None,
                is_push=False,
                is_ping=True,
            )
            mock_get_provider.return_value = mock_provider

            response = webhook_client.post(
                "/webhook/github",
                json={"zen": "test"},
            )

            assert response.status_code == 200
            assert response.json()["status"] == "pong"


class TestMultiProviderRouting:
    """Tests for multi-provider webhook routing."""

    def _mock_provider_push(self, provider_name, mock_get_provider):
        """Set up a mock provider that returns a push event."""
        mock_provider = MagicMock()
        mock_provider.verify_webhook_signature.return_value = True
        mock_provider.parse_webhook_event.return_value = WebhookEvent(
            provider=provider_name,
            event_type="push",
            branch="main",
            commit_sha="abc123",
            sender="dev",
            is_push=True,
            is_ping=False,
        )
        mock_get_provider.return_value = mock_provider
        return mock_provider

    def test_webhook_auto_detection_gitea(self, webhook_client, test_db):
        """Gitea events are auto-detected from X-Gitea-Event header."""
        with patch("fraisier.webhook.get_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.verify_webhook_signature.return_value = True
            mock_provider.parse_webhook_event.return_value = WebhookEvent(
                provider="gitea",
                event_type="ping",
                branch=None,
                commit_sha=None,
                sender=None,
                is_push=False,
                is_ping=True,
            )
            mock_get_provider.return_value = mock_provider

            response = webhook_client.post(
                "/webhook",
                json={"zen": "test"},
                headers={"X-Gitea-Event": "ping"},
            )

            assert response.status_code == 200
            mock_get_provider.assert_called()
            assert mock_get_provider.call_args[0][0] == "gitea"

    def test_webhook_auto_detection_bitbucket(self, webhook_client, test_db):
        """Bitbucket events are auto-detected from X-Event-Key header."""
        with patch("fraisier.webhook.get_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.verify_webhook_signature.return_value = True
            mock_provider.parse_webhook_event.return_value = WebhookEvent(
                provider="bitbucket",
                event_type="ping",
                branch=None,
                commit_sha=None,
                sender=None,
                is_push=False,
                is_ping=True,
            )
            mock_get_provider.return_value = mock_provider

            response = webhook_client.post(
                "/webhook",
                json={"test": "data"},
                headers={"X-Event-Key": "repo:push"},
            )

            assert response.status_code == 200
            mock_get_provider.assert_called()
            assert mock_get_provider.call_args[0][0] == "bitbucket"

    @pytest.mark.parametrize(
        ("provider_name", "header_key", "header_value"),
        [
            ("github", "X-GitHub-Event", "push"),
            ("gitlab", "X-Gitlab-Event", "Push Hook"),
            ("gitea", "X-Gitea-Event", "push"),
            ("bitbucket", "X-Event-Key", "repo:push"),
        ],
    )
    def test_all_providers_route_push_to_deployment(
        self,
        webhook_client,
        test_db,
        provider_name,
        header_key,
        header_value,
        tmp_path,
    ):
        """All four providers route push events through the same deployment path."""

        @contextmanager
        def tmp_lock(fraise_name):
            from fraisier.locking import file_deployment_lock as real_lock

            with real_lock(fraise_name, lock_dir=tmp_path) as path:
                yield path

        with (
            patch("fraisier.webhook.get_provider") as mock_get_provider,
            patch("fraisier.webhook.get_config") as mock_get_config,
            patch("fraisier.webhook.deployment_lock", side_effect=tmp_lock),
        ):
            self._mock_provider_push(provider_name, mock_get_provider)

            mock_config = MagicMock()
            mock_config.get_fraises_for_branch.return_value = [{
                "fraise_name": "my_api",
                "environment": "production",
                "type": "api",
                "app_path": "/tmp/api",
            }]
            mock_config._config = {"git": {}}
            mock_get_config.return_value = mock_config

            response = webhook_client.post(
                "/webhook",
                json={"ref": "refs/heads/main"},
                headers={header_key: header_value},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "deployment_triggered"
            assert data["provider"] == provider_name
            assert data["fraise"] == "my_api"

            # Verify event was recorded in DB with correct provider
            webhooks = test_db.get_recent_webhooks(limit=1)
            assert webhooks[0]["git_provider"] == provider_name

    @pytest.mark.parametrize(
        "provider_name",
        ["github", "gitlab", "gitea", "bitbucket"],
    )
    def test_all_providers_use_same_process_webhook_event(self, provider_name):
        """All providers route through process_webhook_event."""
        event = WebhookEvent(
            provider=provider_name,
            event_type="push",
            branch="main",
            commit_sha="abc123",
            sender="dev",
            is_push=True,
            is_ping=False,
        )

        with patch("fraisier.webhook.get_config") as mock_config:
            mock_config_obj = MagicMock()
            mock_config_obj.get_fraises_for_branch.return_value = [{
                "fraise_name": "my_api",
                "environment": "production",
                "type": "api",
            }]
            mock_config.return_value = mock_config_obj

            from fastapi import BackgroundTasks

            background_tasks = BackgroundTasks()
            result = process_webhook_event(event, background_tasks, webhook_id=1)

            assert result["status"] == "deployment_triggered"
            assert result["provider"] == provider_name


class TestPublicStatusEndpoint:
    """Tests for GET /api/status/{fraise_name} — public, safe fields only."""

    def test_returns_safe_fields_only(self, webhook_client):
        """Public endpoint returns only safe fields."""
        status = DeploymentStatusFile(
            fraise_name="my_api",
            environment="production",
            state="success",
            version="1.2.3",
            commit_sha="abc123def456",
            started_at="2026-03-23T10:00:00Z",
            finished_at="2026-03-23T10:01:00Z",
            error_message=None,
            migration_report={"applied": ["001_init"]},
        )

        with patch("fraisier.webhook.read_status", return_value=status):
            response = webhook_client.get("/api/status/my_api")

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "success"
        assert data["version"] == "1.2.3"
        assert data["commit_sha"] == "abc123def456"
        assert data["environment"] == "production"
        # Sensitive fields must NOT be present
        assert "error_message" not in data
        assert "migration_report" not in data
        assert "last_error" not in data
        assert "started_at" not in data
        assert "finished_at" not in data

    def test_unknown_fraise_returns_404(self, webhook_client):
        """Unknown fraise name returns 404."""
        with patch("fraisier.webhook.read_status", return_value=None):
            response = webhook_client.get("/api/status/nonexistent")

        assert response.status_code == 404

    def test_idle_status(self, webhook_client):
        """Idle fraise returns idle state with null version."""
        status = DeploymentStatusFile(
            fraise_name="my_api",
            environment="production",
            state="idle",
        )

        with patch("fraisier.webhook.read_status", return_value=status):
            response = webhook_client.get("/api/status/my_api")

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "idle"
        assert data["version"] is None


class TestAuthenticatedDetailsEndpoint:
    """Tests for GET /api/status/{fraise_name}/details — requires X-Deployment-Token."""

    _SECRET = "a" * 32  # Minimum length for webhook secret

    def test_valid_token_returns_full_details_on_failure(self, webhook_client):
        """Authenticated details endpoint returns error_message and migration_report."""
        status = DeploymentStatusFile(
            fraise_name="my_api",
            environment="production",
            state="failed",
            version="1.2.3",
            commit_sha="abc123def456",
            error_message="Migration 003 failed: column already exists",
            migration_report={"failed": "003_add_column", "applied": ["001", "002"]},
        )

        with (
            patch("fraisier.webhook.read_status", return_value=status),
            patch.dict(os.environ, {"FRAISIER_WEBHOOK_SECRET": self._SECRET}),
        ):
            response = webhook_client.get(
                "/api/status/my_api/details",
                headers={"X-Deployment-Token": self._SECRET},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "failed"
        assert data["error_message"] == "Migration 003 failed: column already exists"
        assert data["migration_report"]["failed"] == "003_add_column"
        assert data["version"] == "1.2.3"
        assert data["commit_sha"] == "abc123def456"
        assert data["environment"] == "production"

    def test_missing_token_returns_403(self, webhook_client):
        """Missing X-Deployment-Token header returns 403."""
        status = DeploymentStatusFile(
            fraise_name="my_api",
            environment="production",
            state="failed",
            error_message="something broke",
        )

        with (
            patch("fraisier.webhook.read_status", return_value=status),
            patch.dict(os.environ, {"FRAISIER_WEBHOOK_SECRET": self._SECRET}),
        ):
            response = webhook_client.get("/api/status/my_api/details")

        assert response.status_code == 403

    def test_invalid_token_returns_403(self, webhook_client):
        """Wrong X-Deployment-Token returns 403."""
        status = DeploymentStatusFile(
            fraise_name="my_api",
            environment="production",
            state="failed",
            error_message="something broke",
        )

        with (
            patch("fraisier.webhook.read_status", return_value=status),
            patch.dict(os.environ, {"FRAISIER_WEBHOOK_SECRET": self._SECRET}),
        ):
            response = webhook_client.get(
                "/api/status/my_api/details",
                headers={"X-Deployment-Token": "wrong-secret-that-is-long-enough!!"},
            )

        assert response.status_code == 403

    def test_non_failed_status_returns_no_failure(self, webhook_client):
        """Details endpoint on a non-failed deployment returns 'no failure' message."""
        status = DeploymentStatusFile(
            fraise_name="my_api",
            environment="production",
            state="success",
            version="1.2.3",
            commit_sha="abc123def456",
        )

        with (
            patch("fraisier.webhook.read_status", return_value=status),
            patch.dict(os.environ, {"FRAISIER_WEBHOOK_SECRET": self._SECRET}),
        ):
            response = webhook_client.get(
                "/api/status/my_api/details",
                headers={"X-Deployment-Token": self._SECRET},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "success"
        assert "no failure" in data["message"].lower()

    def test_unknown_fraise_returns_404(self, webhook_client):
        """Unknown fraise returns 404 even with valid token."""
        with (
            patch("fraisier.webhook.read_status", return_value=None),
            patch.dict(os.environ, {"FRAISIER_WEBHOOK_SECRET": self._SECRET}),
        ):
            response = webhook_client.get(
                "/api/status/nonexistent/details",
                headers={"X-Deployment-Token": self._SECRET},
            )

        assert response.status_code == 404


class TestWebhookIntegration:
    """Integration tests for webhook handling."""

    def test_full_webhook_flow_github_push(self, webhook_client, test_db):
        """Test complete flow: webhook → parse → record → deploy."""
        with (
            patch("fraisier.webhook.get_provider") as mock_get_provider,
            patch("fraisier.webhook.get_config") as mock_get_config,
        ):
            # Setup provider mock
            mock_provider = MagicMock()
            mock_provider.verify_webhook_signature.return_value = True
            mock_provider.parse_webhook_event.return_value = WebhookEvent(
                provider="github",
                event_type="push",
                branch="main",
                commit_sha="abc123def456",
                sender="developer",
                is_push=True,
                is_ping=False,
            )
            mock_get_provider.return_value = mock_provider

            # Setup config mock (no fraise configured for this branch)
            mock_config = MagicMock()
            mock_config.get_fraises_for_branch.return_value = []
            mock_get_config.return_value = mock_config

            payload = {
                "ref": "refs/heads/main",
                "repository": {"name": "test-repo"},
                "pusher": {"name": "developer"},
            }

            response = webhook_client.post(
                "/webhook",
                json=payload,
                headers={"X-GitHub-Event": "push"},
            )

            # Should complete successfully
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ignored"  # No fraise configured

            # Verify event was recorded
            webhooks = test_db.get_recent_webhooks(limit=1)
            assert len(webhooks) > 0
            assert webhooks[0]["event_type"] == "push"
            assert webhooks[0]["provider"] == "github"
            assert webhooks[0]["processed"] == 0  # Not linked (no deployment)


class TestStructuredErrorResponses:
    """Tests for structured JSON error responses."""

    def test_invalid_signature_returns_structured_error(
        self, webhook_client, sample_webhook_payload
    ):
        """401 returns JSON with error_type, message, and recovery_hint."""
        response = webhook_client.post(
            "/webhook",
            json=sample_webhook_payload,
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": "sha256=invalid",
            },
        )
        assert response.status_code == 401
        data = response.json()
        assert "error_type" in data
        assert "message" in data
        assert "recovery_hint" in data
        assert data["error_type"] == "authentication_error"

    def test_malformed_json_returns_structured_error(self, webhook_client):
        """400 for bad JSON returns structured error."""
        with patch("fraisier.webhook.get_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.verify_webhook_signature.return_value = True
            mock_get_provider.return_value = mock_provider

            response = webhook_client.post(
                "/webhook",
                content=b"not json",
                headers={"X-GitHub-Event": "push"},
            )
        assert response.status_code == 400
        data = response.json()
        assert "error_type" in data
        assert "message" in data
        assert data["error_type"] == "validation_error"

    def test_unknown_provider_returns_structured_error(
        self, webhook_client, sample_webhook_payload
    ):
        """400 for unknown provider returns structured error."""
        response = webhook_client.post(
            "/webhook?provider=unknown",
            json=sample_webhook_payload,
        )
        assert response.status_code == 400
        data = response.json()
        assert "error_type" in data
        assert data["error_type"] == "validation_error"

    def test_status_404_returns_structured_error(self, webhook_client):
        """404 for unknown fraise returns structured error."""
        with patch("fraisier.webhook.read_status", return_value=None):
            response = webhook_client.get("/api/status/nonexistent")

        assert response.status_code == 404
        data = response.json()
        assert "error_type" in data
        assert "message" in data
        assert data["error_type"] == "not_found"

    def test_details_403_returns_structured_error(self, webhook_client):
        """403 for bad token returns structured error."""
        valid_secret = "a" * 32
        status = DeploymentStatusFile(
            fraise_name="my_api",
            environment="production",
            state="failed",
        )

        with (
            patch("fraisier.webhook.read_status", return_value=status),
            patch.dict(os.environ, {"FRAISIER_WEBHOOK_SECRET": valid_secret}),
        ):
            response = webhook_client.get(
                "/api/status/my_api/details",
                headers={"X-Deployment-Token": "wrong-token-that-is-long-enough!!!"},
            )

        assert response.status_code == 403
        data = response.json()
        assert "error_type" in data
        assert data["error_type"] == "authentication_error"
        assert "recovery_hint" in data

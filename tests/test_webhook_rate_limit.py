"""Tests for webhook rate limiting."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from fraisier.webhook import app


class TestWebhookRateLimit:
    """Webhook endpoint must reject requests exceeding rate limit."""

    def test_rate_limited_after_burst(self):
        client = TestClient(app)
        with patch("fraisier.webhook.get_provider") as mock_get_provider:
            mock_provider = MagicMock()
            mock_provider.verify_webhook_signature.return_value = True
            mock_provider.parse_webhook_event.return_value = MagicMock(
                provider="github",
                event_type="ping",
                is_push=False,
                is_ping=True,
                branch=None,
                commit_sha=None,
                sender=None,
            )
            mock_get_provider.return_value = mock_provider

            status_codes = []
            for _ in range(20):
                response = client.post(
                    "/webhook",
                    json={"zen": "test"},
                    headers={"X-GitHub-Event": "ping"},
                )
                status_codes.append(response.status_code)

        assert 429 in status_codes

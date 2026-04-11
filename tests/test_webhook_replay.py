"""Tests for GitHub webhook replay protection (delivery-ID deduplication)."""

import hashlib
import hmac
import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from fraisier.git.github import _delivery_dedupe, _DeliveryDedupe
from fraisier.webhook import app

WEBHOOK_SECRET = "test-webhook-secret-replay-protection-32c"


def _push_body() -> bytes:
    return json.dumps(
        {
            "ref": "refs/heads/main",
            "after": "abc123def456",
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "dev"},
        }
    ).encode()


def _sign_github(
    body: bytes,
    secret: str,
    *,
    delivery_id: str | None = "unique-delivery-id",
) -> dict[str, str]:
    """Return headers for a signed GitHub webhook request."""
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": sig,
    }
    if delivery_id is not None:
        headers["X-GitHub-Delivery"] = delivery_id
    return headers


@pytest.fixture
def client():
    """TestClient with mocked secrets and fresh dedupe state."""
    _delivery_dedupe._store.clear()

    with patch(
        "fraisier.webhook._collect_webhook_secrets", return_value=[WEBHOOK_SECRET]
    ):
        yield TestClient(app)


class TestDeliveryDedupe:
    """Unit tests for _DeliveryDedupe."""

    def test_first_delivery_is_not_seen(self):
        d = _DeliveryDedupe(max_entries=10, ttl_seconds=600)
        assert d.seen("abc-1") is False

    def test_second_delivery_with_same_id_is_seen(self):
        d = _DeliveryDedupe(max_entries=10, ttl_seconds=600)
        d.seen("abc-1")
        assert d.seen("abc-1") is True

    def test_different_delivery_ids_are_not_seen(self):
        d = _DeliveryDedupe(max_entries=10, ttl_seconds=600)
        d.seen("abc-1")
        assert d.seen("abc-2") is False

    def test_expired_entry_is_not_seen(self):
        d = _DeliveryDedupe(max_entries=10, ttl_seconds=1)
        d.seen("abc-expire")
        # Fake expiry by backdating the store entry
        d._store["abc-expire"] = time.time() - 2
        assert d.seen("abc-expire") is False

    def test_evicts_oldest_when_at_capacity(self):
        d = _DeliveryDedupe(max_entries=2, ttl_seconds=600)
        d.seen("a")
        time.sleep(0.01)  # ensure different timestamps
        d.seen("b")
        # At capacity — adding "c" should evict "a" (oldest)
        d.seen("c")
        assert len(d._store) == 2
        assert "a" not in d._store


class TestWebhookReplayProtection:
    """Integration tests for delivery-ID replay protection on /webhook/github."""

    def test_first_delivery_accepted(self, client):
        body = _push_body()
        headers = _sign_github(body, WEBHOOK_SECRET, delivery_id="del-001")
        with patch("fraisier.webhook.get_config") as mock_cfg:
            mock_cfg.side_effect = FileNotFoundError
            resp = client.post("/webhook/github", content=body, headers=headers)
        assert resp.status_code == 200

    def test_replayed_delivery_rejected_409(self, client):
        body = _push_body()
        headers = _sign_github(body, WEBHOOK_SECRET, delivery_id="del-replay")
        with patch("fraisier.webhook.get_config") as mock_cfg:
            mock_cfg.side_effect = FileNotFoundError
            first = client.post("/webhook/github", content=body, headers=headers)
            assert first.status_code == 200
        second = client.post("/webhook/github", content=body, headers=headers)
        assert second.status_code == 409
        assert "replay" in second.json().get("message", "").lower()

    def test_distinct_delivery_ids_both_accepted(self, client):
        body = _push_body()
        with patch("fraisier.webhook.get_config") as mock_cfg:
            mock_cfg.side_effect = FileNotFoundError
            r1 = client.post(
                "/webhook/github",
                content=body,
                headers=_sign_github(body, WEBHOOK_SECRET, delivery_id="del-A"),
            )
            r2 = client.post(
                "/webhook/github",
                content=body,
                headers=_sign_github(body, WEBHOOK_SECRET, delivery_id="del-B"),
            )
        assert r1.status_code == 200
        assert r2.status_code == 200

    def test_missing_delivery_header_rejected_400(self, client):
        body = _push_body()
        headers = _sign_github(body, WEBHOOK_SECRET, delivery_id=None)
        resp = client.post("/webhook/github", content=body, headers=headers)
        assert resp.status_code == 400
        assert "delivery" in resp.json().get("message", "").lower()

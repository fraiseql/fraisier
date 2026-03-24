"""Security tests for webhook signature verification.

All git providers must reject unsigned webhooks when no secret is configured.
"""

from fraisier.git.bitbucket import BitbucketProvider
from fraisier.git.gitea import GiteaProvider
from fraisier.git.github import GitHubProvider
from fraisier.git.gitlab import GitLabProvider


class TestProviderRejectsUnsignedWhenNoSecret:
    """Every provider must return False from verify_webhook_signature when no secret."""

    def test_github_rejects_unsigned_when_no_secret(self):
        provider = GitHubProvider({"webhook_secret": ""})
        assert provider.verify_webhook_signature(b"payload", {}) is False

    def test_github_rejects_unsigned_none_secret(self):
        provider = GitHubProvider({})
        assert provider.verify_webhook_signature(b"payload", {}) is False

    def test_gitlab_rejects_unsigned_when_no_secret(self):
        provider = GitLabProvider({"webhook_secret": ""})
        assert provider.verify_webhook_signature(b"payload", {}) is False

    def test_gitlab_rejects_unsigned_none_secret(self):
        provider = GitLabProvider({})
        assert provider.verify_webhook_signature(b"payload", {}) is False

    def test_gitea_rejects_unsigned_when_no_secret(self):
        provider = GiteaProvider({"webhook_secret": ""})
        assert provider.verify_webhook_signature(b"payload", {}) is False

    def test_gitea_rejects_unsigned_none_secret(self):
        provider = GiteaProvider({})
        assert provider.verify_webhook_signature(b"payload", {}) is False

    def test_bitbucket_rejects_unsigned_when_no_secret(self):
        provider = BitbucketProvider({"webhook_secret": ""})
        assert provider.verify_webhook_signature(b"payload", {}) is False

    def test_bitbucket_rejects_unsigned_none_secret(self):
        provider = BitbucketProvider({})
        assert provider.verify_webhook_signature(b"payload", {}) is False


class TestProviderAcceptsValidSignature:
    """Providers must still accept properly signed webhooks."""

    def test_github_accepts_valid_signature(self):
        import hashlib
        import hmac

        secret = "test-secret"
        payload = b'{"action": "push"}'
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        provider = GitHubProvider({"webhook_secret": secret})
        assert (
            provider.verify_webhook_signature(payload, {"X-Hub-Signature-256": sig})
            is True
        )

    def test_gitlab_accepts_valid_token(self):
        secret = "test-secret"
        provider = GitLabProvider({"webhook_secret": secret})
        assert (
            provider.verify_webhook_signature(b"payload", {"X-Gitlab-Token": secret})
            is True
        )

    def test_gitea_accepts_valid_signature(self):
        import hashlib
        import hmac

        secret = "test-secret"
        payload = b'{"action": "push"}'
        sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        provider = GiteaProvider({"webhook_secret": secret})
        assert (
            provider.verify_webhook_signature(payload, {"X-Gitea-Signature": sig})
            is True
        )

    def test_bitbucket_accepts_valid_signature(self):
        import hashlib
        import hmac

        secret = "test-secret"
        payload = b'{"action": "push"}'
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        provider = BitbucketProvider({"webhook_secret": secret, "server": True})
        assert (
            provider.verify_webhook_signature(payload, {"X-Hub-Signature": sig}) is True
        )

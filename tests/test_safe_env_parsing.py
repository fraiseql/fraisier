"""Tests for safe environment variable integer parsing."""

import importlib

import pytest

from fraisier._env import get_int_env


class TestGetIntEnv:
    """get_int_env returns safe defaults for malformed values."""

    def test_valid_integer_accepted(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "42")
        assert get_int_env("TEST_VAR", default=10) == 42

    def test_missing_var_returns_default(self):
        assert get_int_env("NONEXISTENT_VAR_XYZ", default=10) == 10

    def test_non_numeric_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "abc")
        assert get_int_env("TEST_VAR", default=10) == 10

    def test_negative_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "-1")
        assert get_int_env("TEST_VAR", default=10, min_value=0) == 10

    def test_zero_falls_back_when_min_is_one(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "0")
        assert get_int_env("TEST_VAR", default=10, min_value=1) == 10

    def test_zero_accepted_when_min_is_zero(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "0")
        assert get_int_env("TEST_VAR", default=10, min_value=0) == 0

    def test_empty_string_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "")
        assert get_int_env("TEST_VAR", default=10) == 10

    def test_float_string_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "3.14")
        assert get_int_env("TEST_VAR", default=10) == 10


class TestWebhookRateLimitEnv:
    """webhook_rate_limit uses safe parsing for FRAISIER_WEBHOOK_RATE_LIMIT."""

    @pytest.fixture(autouse=True)
    def _restore_module(self, monkeypatch):
        """Reload module to default after each test."""
        yield
        monkeypatch.delenv("FRAISIER_WEBHOOK_RATE_LIMIT", raising=False)
        importlib.reload(importlib.import_module("fraisier.webhook_rate_limit"))

    def test_malformed_rate_limit_uses_default(self, monkeypatch):
        monkeypatch.setenv("FRAISIER_WEBHOOK_RATE_LIMIT", "abc")
        import fraisier.webhook_rate_limit as mod

        importlib.reload(mod)
        assert mod._RATE_LIMIT == 10

    def test_negative_rate_limit_uses_default(self, monkeypatch):
        monkeypatch.setenv("FRAISIER_WEBHOOK_RATE_LIMIT", "-1")
        import fraisier.webhook_rate_limit as mod

        importlib.reload(mod)
        assert mod._RATE_LIMIT == 10

    def test_zero_rate_limit_uses_default(self, monkeypatch):
        monkeypatch.setenv("FRAISIER_WEBHOOK_RATE_LIMIT", "0")
        import fraisier.webhook_rate_limit as mod

        importlib.reload(mod)
        assert mod._RATE_LIMIT == 10

    def test_valid_rate_limit_accepted(self, monkeypatch):
        monkeypatch.setenv("FRAISIER_WEBHOOK_RATE_LIMIT", "20")
        import fraisier.webhook_rate_limit as mod

        importlib.reload(mod)
        assert mod._RATE_LIMIT == 20


class TestDbFactoryEnv:
    """DatabaseConfig uses safe parsing for pool size env vars."""

    def test_malformed_pool_min_uses_default(self, monkeypatch):
        monkeypatch.setenv("FRAISIER_DB_POOL_MIN", "abc")
        from fraisier.db.factory import DatabaseConfig

        config = DatabaseConfig()
        assert config.pool_min_size == 1

    def test_malformed_pool_max_uses_default(self, monkeypatch):
        monkeypatch.setenv("FRAISIER_DB_POOL_MAX", "abc")
        from fraisier.db.factory import DatabaseConfig

        config = DatabaseConfig()
        assert config.pool_max_size == 10

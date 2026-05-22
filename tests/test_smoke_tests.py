"""Tests for the authenticated smoke test runner (#204 PR B).

After ``/health`` passes, the deploy pipeline runs configured HTTP
requests with bearer credentials and JSONPath assertions. Failure
default is ``rollback`` — that's the whole point: unauthenticated
``/health`` missed the regression, so if the authenticated probe fails
too, the new code is broken.

Hand-rolled JSONPath supports ``$.dotted.path`` only — no ``$..foo``
recursion, no array filters, no wildcards. Reject the unsupported
shapes at schema parse time.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fraisier.errors import ConfigurationError
from fraisier.smoke_tests import (
    _MISSING,
    Assertion,
    SmokeTest,
    SmokeTestError,
    _walk_json_path,
    load_smoke_tests,
    run_smoke_tests,
)

# ---------------------------------------------------------------------------
# JSONPath walker
# ---------------------------------------------------------------------------


class TestWalkJsonPath:
    def test_walks_nested_keys(self):
        doc = {"data": {"me": {"role": "admin"}}}
        assert _walk_json_path(doc, "$.data.me.role") == "admin"

    def test_returns_missing_sentinel_for_missing_key(self):
        doc = {"data": {"me": {"role": "admin"}}}
        assert _walk_json_path(doc, "$.data.me.email") is _MISSING

    def test_returns_missing_when_intermediate_is_not_a_dict(self):
        doc = {"data": "not-a-dict"}
        assert _walk_json_path(doc, "$.data.me") is _MISSING

    def test_root_only_path_returns_doc(self):
        doc = {"a": 1}
        assert _walk_json_path(doc, "$") == doc

    def test_path_must_start_with_dollar(self):
        with pytest.raises(ConfigurationError, match=r"must start with \$"):
            _walk_json_path({}, "data.me")


# ---------------------------------------------------------------------------
# Assertion semantics
# ---------------------------------------------------------------------------


class TestAssertionMatches:
    def test_not_null_passes_for_value(self):
        a = Assertion(json_path="$.x", not_null=True)
        assert a.matches({"x": "anything"}) is True

    def test_not_null_fails_for_missing_key(self):
        a = Assertion(json_path="$.x", not_null=True)
        assert a.matches({}) is False

    def test_not_null_fails_for_null_value(self):
        # JSON null is loaded as Python None; not_null must reject it.
        a = Assertion(json_path="$.x", not_null=True)
        assert a.matches({"x": None}) is False

    def test_null_passes_for_missing_key(self):
        # The plan's intent: `null: true` is satisfied when the key is
        # absent or its value is JSON null. This is how "no errors"
        # assertions read in practice — `$.errors` simply not in the
        # response is the success case.
        a = Assertion(json_path="$.errors", null=True)
        assert a.matches({}) is True
        assert a.matches({"errors": None}) is True

    def test_null_fails_for_present_non_null_value(self):
        a = Assertion(json_path="$.errors", null=True)
        assert a.matches({"errors": ["boom"]}) is False

    def test_equals_uses_python_equality(self):
        a = Assertion(json_path="$.role", equals="admin")
        assert a.matches({"role": "admin"}) is True
        assert a.matches({"role": "user"}) is False
        # Numeric equality through JSON.
        a_int = Assertion(json_path="$.n", equals=42)
        assert a_int.matches({"n": 42}) is True
        assert a_int.matches({"n": "42"}) is False


# ---------------------------------------------------------------------------
# Loader / URL resolution
# ---------------------------------------------------------------------------


class TestLoadSmokeTests:
    def _entry(self, **overrides: Any) -> dict:
        base = {
            "name": "authenticated_me",
            "method": "POST",
            "url": "/graphql",
            "timeout": 5,
            "headers": {"Authorization": "Bearer token"},
            "body": '{"query":"{ me { id } }"}',
            "assert": [
                {"json_path": "$.data.me.id", "not_null": True},
                {"json_path": "$.errors", "null": True},
            ],
        }
        base.update(overrides)
        return base

    def test_loads_happy_path(self):
        env_config = {
            "smoke_tests": [self._entry()],
            "health_check": {"url": "https://api.example.com/health"},
        }
        tests = load_smoke_tests(env_config, base_url="https://api.example.com")
        assert len(tests) == 1
        t = tests[0]
        assert isinstance(t, SmokeTest)
        assert t.name == "authenticated_me"
        assert t.url == "https://api.example.com/graphql"
        assert t.on_failure == "rollback"  # default
        assert len(t.assertions) == 2

    def test_token_provider_defaults_to_none_when_absent(self):
        env_config = {
            "smoke_tests": [self._entry()],
            "health_check": {"url": "https://api.example.com/health"},
        }
        tests = load_smoke_tests(env_config, base_url="https://api.example.com")
        assert tests[0].token_provider is None

    def test_absolute_url_is_preserved(self):
        env_config = {
            "smoke_tests": [self._entry(url="https://other.example/api")],
        }
        tests = load_smoke_tests(env_config, base_url="https://api.example.com")
        assert tests[0].url == "https://other.example/api"

    def test_relative_url_requires_base_url(self):
        env_config = {"smoke_tests": [self._entry(url="/graphql")]}
        with pytest.raises(ConfigurationError, match=r"relative.*health_check"):
            load_smoke_tests(env_config, base_url=None)

    def test_rejects_unknown_method(self):
        env_config = {"smoke_tests": [self._entry(method="MERGE")]}
        with pytest.raises(ConfigurationError, match=r"method.*MERGE"):
            load_smoke_tests(env_config, base_url="https://api.example.com")

    def test_rejects_unknown_on_failure(self):
        env_config = {"smoke_tests": [self._entry(on_failure="explode")]}
        with pytest.raises(ConfigurationError, match=r"on_failure"):
            load_smoke_tests(env_config, base_url="https://api.example.com")

    def test_rejects_unknown_assertion_key(self):
        env_config = {
            "smoke_tests": [
                self._entry(**{"assert": [{"json_path": "$.x", "regex": "^foo$"}]})
            ]
        }
        with pytest.raises(ConfigurationError, match=r"unknown assertion key"):
            load_smoke_tests(env_config, base_url="https://api.example.com")

    @pytest.mark.parametrize("bad_path", ["$..foo", "$.a[0]", "$.*", "$.@.x"])
    def test_rejects_unsupported_jsonpath_syntax(self, bad_path):
        env_config = {
            "smoke_tests": [
                self._entry(**{"assert": [{"json_path": bad_path, "not_null": True}]})
            ]
        }
        with pytest.raises(
            ConfigurationError, match=r"unsupported JSONPath.*\$\.dotted\.path only"
        ):
            load_smoke_tests(env_config, base_url="https://api.example.com")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _smoke_test(**overrides) -> SmokeTest:
    base = {
        "name": "t",
        "method": "POST",
        "url": "https://api.example.com/graphql",
        "headers": {"Authorization": "Bearer token"},
        "body": '{"query":"{ me { id } }"}',
        "timeout": 5,
        "on_failure": "rollback",
        "assertions": [
            Assertion(json_path="$.data.me.id", not_null=True),
        ],
    }
    base.update(overrides)
    return SmokeTest(**base)


class TestRunSmokeTests:
    def test_runs_request_and_asserts_response(self):
        with patch("fraisier.smoke_tests.httpx.Client") as mock_client_cls:
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = {"data": {"me": {"id": "u1"}}}
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.request.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            # Must not raise.
            run_smoke_tests([_smoke_test()])

    def test_failing_assertion_raises_with_path_and_actual(self):
        with patch("fraisier.smoke_tests.httpx.Client") as mock_client_cls:
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = {"data": {"me": None}}
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.request.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            with pytest.raises(SmokeTestError) as exc_info:
                run_smoke_tests([_smoke_test()])
            assert "$.data.me.id" in str(exc_info.value)

    def test_non_2xx_response_fails_before_assertion(self):
        with patch("fraisier.smoke_tests.httpx.Client") as mock_client_cls:
            mock_resp = MagicMock(status_code=500)
            mock_resp.json.return_value = {"data": {"me": {"id": "u1"}}}
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.request.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            with pytest.raises(SmokeTestError, match=r"500"):
                run_smoke_tests([_smoke_test()])

    def test_timeout_treated_as_failure(self):
        import httpx

        with patch("fraisier.smoke_tests.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.request.side_effect = httpx.TimeoutException("timeout")
            mock_client_cls.return_value = mock_client

            with pytest.raises(SmokeTestError, match=r"timeout"):
                run_smoke_tests([_smoke_test()])

    def test_authorization_header_redacted_from_logs(self, caplog):
        with (
            patch("fraisier.smoke_tests.httpx.Client") as mock_client_cls,
            caplog.at_level(logging.INFO, logger="fraisier.smoke_tests"),
        ):
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = {"data": {"me": {"id": "u1"}}}
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.request.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            run_smoke_tests([_smoke_test()])

        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "Bearer token" not in joined

    def test_on_failure_warn_does_not_raise(self, caplog):
        with (
            patch("fraisier.smoke_tests.httpx.Client") as mock_client_cls,
            caplog.at_level(logging.WARNING, logger="fraisier.smoke_tests"),
        ):
            mock_resp = MagicMock(status_code=500)
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.request.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            # Must not raise.
            run_smoke_tests([_smoke_test(on_failure="warn")])

        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "500" in joined

    def test_on_failure_halt_raises_with_halt_marker(self):
        with patch("fraisier.smoke_tests.httpx.Client") as mock_client_cls:
            mock_resp = MagicMock(status_code=500)
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.request.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            with pytest.raises(SmokeTestError) as exc_info:
                run_smoke_tests([_smoke_test(on_failure="halt")])
            assert exc_info.value.rollback is False

"""Tests for the pluggable token-provider config layer (#215).

Phase 1 covers the parse skeleton: unknown ``type`` rejection and the
back-compat default (absence of ``token_provider:`` means today's
behavior).
"""

from __future__ import annotations

import pytest

from fraisier.errors import ConfigurationError
from fraisier.smoke_tests import load_smoke_tests
from fraisier.token_providers import _VALID_PROVIDER_TYPES, parse_token_provider


class TestParseTokenProvider:
    def test_unknown_type_raises_configuration_error(self):
        with pytest.raises(ConfigurationError) as exc_info:
            parse_token_provider({"type": "nope"})
        msg = str(exc_info.value)
        assert "nope" in msg
        for valid_type in _VALID_PROVIDER_TYPES:
            assert valid_type in msg

    def test_missing_type_raises_configuration_error(self):
        with pytest.raises(ConfigurationError, match=r"token_provider.*type"):
            parse_token_provider({"command": ["echo", "x"]})


class TestLoadSmokeTestsWithTokenProvider:
    def _entry_with_provider(self, provider: dict) -> dict:
        return {
            "name": "with_provider",
            "method": "POST",
            "url": "/graphql",
            "timeout": 5,
            "token_provider": provider,
            "body": '{"query":"{ me { id } }"}',
            "assert": [{"json_path": "$.data.me.id", "not_null": True}],
        }

    def test_unknown_type_propagates_as_configuration_error(self):
        env_config = {"smoke_tests": [self._entry_with_provider({"type": "nope"})]}
        with pytest.raises(ConfigurationError, match=r"nope"):
            load_smoke_tests(env_config, base_url="https://api.example.com")


class TestHeaderCollisionRejection:
    """A test that declares both ``headers.Authorization`` and a
    ``token_provider`` writing to ``Authorization`` is rejected at
    parse time — silent overrides are exactly what the validator should
    catch.

    Header comparison is case-insensitive per RFC 7230, so spelling one
    side ``authorization`` does not unlock the collision.
    """

    def _entry(self, *, headers: dict, provider_header: str = "Authorization") -> dict:
        return {
            "name": "collision",
            "url": "/me",
            "headers": headers,
            # Phase 1 does not yet wire a working provider type, so an
            # unknown ``type`` would short-circuit before the collision
            # check. Phase 2 adds ``exec`` and this fixture switches to
            # it. For Phase 1 we monkey-patch _VALID_PROVIDER_TYPES in
            # the test to include a sentinel that survives parsing.
            "token_provider": {"type": "_test_phase1", "header": provider_header},
            "assert": [],
        }

    def test_same_case_collision_rejected(self, monkeypatch):
        from fraisier import token_providers

        monkeypatch.setattr(
            token_providers,
            "_VALID_PROVIDER_TYPES",
            frozenset(token_providers._VALID_PROVIDER_TYPES | {"_test_phase1"}),
        )
        env_config = {
            "smoke_tests": [self._entry(headers={"Authorization": "Bearer x"})],
            "health_check": {"url": "https://api.example.com/health"},
        }
        with pytest.raises(ConfigurationError, match=r"collision.*Authorization"):
            load_smoke_tests(env_config, base_url="https://api.example.com")

    def test_case_insensitive_collision_rejected(self, monkeypatch):
        from fraisier import token_providers

        monkeypatch.setattr(
            token_providers,
            "_VALID_PROVIDER_TYPES",
            frozenset(token_providers._VALID_PROVIDER_TYPES | {"_test_phase1"}),
        )
        env_config = {
            "smoke_tests": [
                self._entry(
                    headers={"authorization": "Bearer x"},
                    provider_header="Authorization",
                )
            ],
            "health_check": {"url": "https://api.example.com/health"},
        }
        with pytest.raises(ConfigurationError, match=r"collision"):
            load_smoke_tests(env_config, base_url="https://api.example.com")

    def test_non_overlapping_headers_accepted(self, monkeypatch):
        from fraisier import token_providers

        monkeypatch.setattr(
            token_providers,
            "_VALID_PROVIDER_TYPES",
            frozenset(token_providers._VALID_PROVIDER_TYPES | {"_test_phase1"}),
        )
        env_config = {
            "smoke_tests": [
                self._entry(
                    headers={"X-Tenant": "abc"},
                    provider_header="Authorization",
                )
            ],
            "health_check": {"url": "https://api.example.com/health"},
        }
        tests = load_smoke_tests(env_config, base_url="https://api.example.com")
        assert tests[0].token_provider is not None
        assert tests[0].headers == {"X-Tenant": "abc"}


class TestBackCompatNoProvider:
    """Absence of ``token_provider`` keeps v0.21.x behavior: the static
    ``headers.Authorization`` is sent verbatim to httpx with no provider
    resolution step in between.
    """

    def test_static_authorization_sent_verbatim(self):
        import httpx

        from fraisier.smoke_tests import SmokeTest, run_smoke_tests

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)

        test = SmokeTest(
            name="legacy",
            method="GET",
            url="https://api.example.com/me",
            headers={"Authorization": "Bearer static-jwt"},
            body=None,
            timeout=5,
            on_failure="rollback",
            assertions=[],
            token_provider=None,
        )

        from unittest.mock import patch

        original_client = httpx.Client

        def make_client(*args, **kwargs):
            kwargs["transport"] = transport
            return original_client(*args, **kwargs)

        with patch("fraisier.smoke_tests.httpx.Client", side_effect=make_client):
            run_smoke_tests([test])

        assert len(captured) == 1
        assert captured[0].headers["Authorization"] == "Bearer static-jwt"


class TestValidatorCatchesConfigurationError:
    """The config-load validator must not let ConfigurationError escape.

    ``_validate_smoke_tests`` is a thin wrapper that surfaces loader
    failures as an entry in the validator's error list. After upgrading
    the loader to ``ConfigurationError`` the validator's catch must
    cover both classes.
    """

    def test_returns_error_list_for_unknown_provider_type(self):
        from fraisier.config._validation import _validate_smoke_tests

        env = {
            "smoke_tests": [
                {
                    "name": "with_provider",
                    "url": "/me",
                    "token_provider": {"type": "nope"},
                    "assert": [],
                }
            ],
            "health_check": {"url": "https://api.example.com/health"},
        }
        errors = _validate_smoke_tests("my_api", env)
        assert errors  # not empty
        assert any("nope" in e for e in errors)

    def test_returns_error_list_for_legacy_configuration_error(self):
        from fraisier.config._validation import _validate_smoke_tests

        env = {
            "smoke_tests": [
                {"name": "t", "method": "MERGE", "url": "/me", "assert": []}
            ],
            "health_check": {"url": "https://api.example.com/health"},
        }
        errors = _validate_smoke_tests("my_api", env)
        assert errors
        assert any("MERGE" in e for e in errors)

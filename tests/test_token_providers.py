"""Tests for the pluggable token-provider config layer (#215).

Phase 1 covers the parse skeleton: unknown ``type`` rejection and the
back-compat default (absence of ``token_provider:`` means today's
behavior).
"""

from __future__ import annotations

import pytest

from fraisier.errors import ConfigurationError, DeploymentError
from fraisier.smoke_tests import load_smoke_tests
from fraisier.token_providers import (
    _VALID_PROVIDER_TYPES,
    TokenProvider,
    parse_token_provider,
)


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

    def test_phase2_valid_types_include_exec(self):
        assert "exec" in _VALID_PROVIDER_TYPES


class TestExecProviderParseSideEffects:
    """``parse_token_provider`` must be purely structural. ``fraisier
    validate`` parses the config but must not shell out — a network
    fetch or subprocess from a YAML parse would be a fresh footgun.
    """

    def test_parse_does_not_invoke_subprocess(self, monkeypatch):
        called = []

        def fake_run(*args, **kwargs):
            called.append(args)
            raise AssertionError("subprocess.run was called during parse")

        monkeypatch.setattr("fraisier.token_providers.subprocess.run", fake_run)

        provider = parse_token_provider({"type": "exec", "command": ["false"]})
        # Parse succeeded without running anything.
        assert called == []
        assert provider.command == ("false",)

    def test_validate_does_not_invoke_subprocess(self, monkeypatch):
        from fraisier.config._validation import _validate_smoke_tests

        called = []

        def fake_run(*args, **kwargs):
            called.append(args)
            raise AssertionError("subprocess.run was called during validate")

        monkeypatch.setattr("fraisier.token_providers.subprocess.run", fake_run)

        env = {
            "smoke_tests": [
                {
                    "name": "t",
                    "url": "/me",
                    "token_provider": {"type": "exec", "command": ["false"]},
                    "assert": [],
                }
            ],
            "health_check": {"url": "https://api.example.com/health"},
        }
        errors = _validate_smoke_tests("my_api", env)
        assert errors == []
        assert called == []


class TestExecProvider:
    def test_returns_stdout_trimmed_of_trailing_newline(self):
        provider = parse_token_provider(
            {"type": "exec", "command": ["printf", "abc\\n"]}
        )
        assert provider.resolve() == "abc"

    def test_returns_stdout_when_no_trailing_newline(self):
        provider = parse_token_provider({"type": "exec", "command": ["printf", "abc"]})
        assert provider.resolve() == "abc"

    def test_preserves_internal_whitespace_and_equals_padding(self):
        # Opaque tokens may contain `=` padding or spaces — strip only
        # the trailing newline.
        provider = parse_token_provider(
            {"type": "exec", "command": ["printf", "abc==\\n"]}
        )
        assert provider.resolve() == "abc=="

    def test_command_stored_as_tuple(self):
        provider = parse_token_provider({"type": "exec", "command": ["printf", "abc"]})
        assert isinstance(provider, TokenProvider)
        assert provider.type == "exec"

    def test_non_zero_exit_raises_deployment_error(self):
        from fraisier.smoke_tests import SmokeTestError

        provider = parse_token_provider({"type": "exec", "command": ["false"]})
        with pytest.raises(DeploymentError) as exc_info:
            provider.resolve()
        msg = str(exc_info.value)
        assert "exec" in msg
        assert "false" in msg
        assert "1" in msg  # exit code of `false`
        # Critical: must NOT be SmokeTestError — the deployer treats
        # SmokeTestError as a smoke-test-policy failure (rollback/halt
        # /warn), which is the wrong response to a token-fetch failure.
        assert not isinstance(exc_info.value, SmokeTestError)

    def test_non_zero_exit_includes_stderr_tail(self):
        # A script that writes context to stderr and exits non-zero.
        provider = parse_token_provider(
            {
                "type": "exec",
                "command": [
                    "sh",
                    "-c",
                    "echo 'something went wrong' >&2; exit 7",
                ],
            }
        )
        with pytest.raises(DeploymentError, match=r"something went wrong"):
            provider.resolve()

    @pytest.mark.skipif(
        __import__("sys").platform.startswith("win"), reason="POSIX-only signals"
    )
    def test_timeout_raises_deployment_error(self):
        provider = parse_token_provider(
            {"type": "exec", "command": ["sleep", "5"], "timeout": 1}
        )
        with pytest.raises(DeploymentError, match=r"timed out"):
            provider.resolve()

    def test_info_logs_argv0_but_not_remaining_args(self, caplog):
        import contextlib
        import logging as _logging

        provider = parse_token_provider(
            {
                "type": "exec",
                "command": ["printf", "--client-id", "abc123"],
            }
        )
        with (
            caplog.at_level(_logging.INFO, logger="fraisier.token_providers"),
            contextlib.suppress(DeploymentError),
        ):
            # printf with -- flag may error; we only care about logs.
            provider.resolve()

        # Filter to INFO-and-above (caplog.at_level captures DEBUG even
        # when the threshold is INFO, but the records carry their real
        # levelno).
        info_messages = [
            rec.getMessage() for rec in caplog.records if rec.levelno >= _logging.INFO
        ]
        joined = "\n".join(info_messages)
        assert "printf" in joined
        assert "--client-id" not in joined
        assert "abc123" not in joined

    def test_debug_logs_full_argv(self, caplog):
        import logging as _logging

        provider = parse_token_provider(
            {
                "type": "exec",
                "command": ["printf", "ok"],
            }
        )
        with caplog.at_level(_logging.DEBUG, logger="fraisier.token_providers"):
            provider.resolve()

        debug_messages = [
            rec.getMessage() for rec in caplog.records if rec.levelno == _logging.DEBUG
        ]
        joined = "\n".join(debug_messages)
        assert "printf" in joined
        assert "ok" in joined  # full argv visible at DEBUG

    def test_resolved_token_never_appears_in_logs(self, caplog, tmp_path):
        import logging as _logging

        # Put the secret in a file the script reads — argv has only the
        # path. This models realistic usage where the resolved token
        # comes from outside argv (env var, file, IdP call, etc.).
        secret = "super-secret-jwt-9f3a"
        secret_file = tmp_path / "secret.txt"
        secret_file.write_text(secret)

        provider = parse_token_provider(
            {"type": "exec", "command": ["cat", str(secret_file)]}
        )
        # Capture EVERY record at every level — leakage at DEBUG counts.
        with caplog.at_level(_logging.DEBUG, logger="fraisier.token_providers"):
            assert provider.resolve() == secret

        for rec in caplog.records:
            assert secret not in rec.getMessage(), (
                f"token leaked at level={rec.levelname} message={rec.getMessage()!r}"
            )


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
            "token_provider": {
                "type": "exec",
                "command": ["echo", "x"],
                "header": provider_header,
            },
            "assert": [],
        }

    def test_same_case_collision_rejected(self):
        env_config = {
            "smoke_tests": [self._entry(headers={"Authorization": "Bearer x"})],
            "health_check": {"url": "https://api.example.com/health"},
        }
        with pytest.raises(ConfigurationError, match=r"collision.*Authorization"):
            load_smoke_tests(env_config, base_url="https://api.example.com")

    def test_case_insensitive_collision_rejected(self):
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

    def test_non_overlapping_headers_accepted(self):
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


class TestOauth2ClientCredentialsProvider:
    """OIDC client-credentials grant: POST to token endpoint with
    client_id+secret, use the returned ``access_token``.
    """

    def _provider(self, **overrides) -> dict:
        base = {
            "type": "oauth2_client_credentials",
            "token_url": "https://idp.example.com/oauth/token",
            "client_id": "deploy-client",
            "client_secret": "shh",
        }
        base.update(overrides)
        return base

    def test_posts_form_body_and_returns_access_token(self, monkeypatch):
        import httpx

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = request.content.decode()
            captured["method"] = request.method
            return httpx.Response(
                200,
                json={
                    "access_token": "abc-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            "fraisier.token_providers._oauth2_http_transport",
            lambda: transport,
            raising=False,
        )

        provider = parse_token_provider(
            self._provider(audience="https://api.example.com", scope="read write")
        )
        assert provider.resolve() == "abc-token"
        assert captured["method"] == "POST"
        assert captured["url"] == "https://idp.example.com/oauth/token"
        # Form body is x-www-form-urlencoded.
        body = captured["body"]
        assert "grant_type=client_credentials" in body
        assert "client_id=deploy-client" in body
        assert "client_secret=shh" in body
        assert "audience=https%3A%2F%2Fapi.example.com" in body
        assert "scope=read+write" in body

    def test_non_2xx_aborts_with_deployment_error(self, monkeypatch):
        import httpx

        captured_body: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_body["body"] = request.content.decode()
            # Some IdPs echo client_secret in error responses — defend
            # by including it in the body to confirm we do NOT surface
            # it in the raised message.
            return httpx.Response(
                401,
                json={
                    "error": "invalid_client",
                    "error_description": "client_secret was 'shh' (echoed)",
                },
            )

        monkeypatch.setattr(
            "fraisier.token_providers._oauth2_http_transport",
            lambda: httpx.MockTransport(handler),
            raising=False,
        )

        provider = parse_token_provider(self._provider())
        with pytest.raises(DeploymentError) as exc_info:
            provider.resolve()
        msg = str(exc_info.value)
        assert "401" in msg
        assert "oauth2_client_credentials" in msg
        # Body must not leak into the exception message.
        assert "shh" not in msg

    def test_missing_access_token_aborts_with_deployment_error(self, monkeypatch):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"token_type": "Bearer"})

        monkeypatch.setattr(
            "fraisier.token_providers._oauth2_http_transport",
            lambda: httpx.MockTransport(handler),
            raising=False,
        )

        provider = parse_token_provider(self._provider())
        with pytest.raises(DeploymentError, match=r"access_token"):
            provider.resolve()

    def test_client_secret_never_appears_in_logs(self, caplog, monkeypatch):
        import logging as _logging

        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "ok"})

        monkeypatch.setattr(
            "fraisier.token_providers._oauth2_http_transport",
            lambda: httpx.MockTransport(handler),
            raising=False,
        )

        secret = "ULTRA-secret-7q"
        provider = parse_token_provider(self._provider(client_secret=secret))
        with caplog.at_level(_logging.DEBUG, logger="fraisier.token_providers"):
            assert provider.resolve() == "ok"

        for rec in caplog.records:
            assert secret not in rec.getMessage(), (
                f"client_secret leaked at level={rec.levelname}: {rec.getMessage()!r}"
            )

    def test_required_fields_validated_at_parse_time(self):
        with pytest.raises(ConfigurationError, match=r"token_url"):
            parse_token_provider(
                {
                    "type": "oauth2_client_credentials",
                    "client_id": "x",
                    "client_secret": "y",
                }
            )
        with pytest.raises(ConfigurationError, match=r"client_id"):
            parse_token_provider(
                {
                    "type": "oauth2_client_credentials",
                    "token_url": "https://idp/x",
                    "client_secret": "y",
                }
            )
        with pytest.raises(ConfigurationError, match=r"client_secret"):
            parse_token_provider(
                {
                    "type": "oauth2_client_credentials",
                    "token_url": "https://idp/x",
                    "client_id": "x",
                }
            )


class TestOauth2RefreshTokenProvider:
    """OIDC ``grant_type=refresh_token`` flow. Same failure modes as
    client_credentials (shared helper). Any rotated ``refresh_token``
    in the response is discarded — persistence is out of scope.
    """

    def _provider(self, **overrides) -> dict:
        base = {
            "type": "oauth2_refresh_token",
            "token_url": "https://idp.example.com/oauth/token",
            "client_id": "deploy-client",
            "refresh_token": "rt-abc",
        }
        base.update(overrides)
        return base

    def test_posts_refresh_grant_and_returns_access_token(self, monkeypatch):
        import httpx

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "new-access"})

        monkeypatch.setattr(
            "fraisier.token_providers._oauth2_http_transport",
            lambda: httpx.MockTransport(handler),
            raising=False,
        )

        provider = parse_token_provider(self._provider())
        assert provider.resolve() == "new-access"
        body = captured["body"]
        assert "grant_type=refresh_token" in body
        assert "client_id=deploy-client" in body
        assert "refresh_token=rt-abc" in body

    def test_rotated_refresh_token_in_response_is_discarded(self, caplog, monkeypatch):
        import logging as _logging

        import httpx

        rotated = "rotated-rt-xyz"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": rotated,
                },
            )

        monkeypatch.setattr(
            "fraisier.token_providers._oauth2_http_transport",
            lambda: httpx.MockTransport(handler),
            raising=False,
        )

        provider = parse_token_provider(self._provider())
        with caplog.at_level(_logging.DEBUG, logger="fraisier.token_providers"):
            assert provider.resolve() == "new-access"

        # The rotated refresh token must not appear in any log line.
        for rec in caplog.records:
            assert rotated not in rec.getMessage(), (
                f"rotated refresh_token leaked: {rec.getMessage()!r}"
            )

    def test_non_2xx_aborts(self, monkeypatch):
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        monkeypatch.setattr(
            "fraisier.token_providers._oauth2_http_transport",
            lambda: httpx.MockTransport(handler),
            raising=False,
        )

        provider = parse_token_provider(self._provider())
        with pytest.raises(DeploymentError, match=r"400"):
            provider.resolve()

    def test_refresh_token_never_leaks_to_logs(self, caplog, monkeypatch):
        import logging as _logging

        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "ok"})

        monkeypatch.setattr(
            "fraisier.token_providers._oauth2_http_transport",
            lambda: httpx.MockTransport(handler),
            raising=False,
        )

        secret_rt = "RT-ULTRA-secret-9q"
        provider = parse_token_provider(self._provider(refresh_token=secret_rt))
        with caplog.at_level(_logging.DEBUG, logger="fraisier.token_providers"):
            assert provider.resolve() == "ok"

        for rec in caplog.records:
            assert secret_rt not in rec.getMessage(), (
                f"refresh_token leaked at level={rec.levelname}: {rec.getMessage()!r}"
            )

    def test_required_fields_validated_at_parse_time(self):
        with pytest.raises(ConfigurationError, match=r"refresh_token"):
            parse_token_provider(
                {
                    "type": "oauth2_refresh_token",
                    "token_url": "https://idp/x",
                    "client_id": "x",
                }
            )


class TestProviderResolvesOncePerDeploy:
    """When N smoke tests share one ``token_provider`` config block,
    the provider's ``.resolve()`` runs exactly once and all N tests
    receive the same token. The cache key is ``id(provider)`` — the
    provider object is constructed once during ``load_smoke_tests``,
    so identity is stable across the materialization pass.
    """

    def test_shared_provider_resolves_exactly_once(self):
        from unittest.mock import patch

        from fraisier.smoke_tests import materialize_test_headers

        env_config = {
            "smoke_tests": [
                {
                    "name": f"t{i}",
                    "url": f"/path-{i}",
                    "token_provider": {
                        "type": "exec",
                        "command": ["printf", "tok"],
                    },
                    "assert": [],
                }
                for i in range(3)
            ],
            "health_check": {"url": "https://api.example.com/health"},
        }

        # Make all three entries share the same parsed provider by
        # post-loading. (load_smoke_tests builds three distinct
        # TokenProvider instances; the cache-on-id() contract is what
        # the deploy pipeline relies on when YAML anchors are used to
        # share the block.)
        tests = load_smoke_tests(env_config, base_url="https://api.example.com")
        shared = tests[0].token_provider
        assert shared is not None
        from dataclasses import replace as _replace

        tests = [_replace(t, token_provider=shared) for t in tests]

        run_count = 0
        original_run = __import__("subprocess").run

        def counting_run(*args, **kwargs):
            nonlocal run_count
            run_count += 1
            return original_run(*args, **kwargs)

        with patch("fraisier.token_providers.subprocess.run", side_effect=counting_run):
            materialized = materialize_test_headers(tests)

        assert run_count == 1
        # All three tests received the same materialized Authorization.
        assert len({t.headers["Authorization"] for t in materialized}) == 1


class TestProviderHeaderSubstitution:
    """End-to-end: a smoke test bound to an ``exec`` provider gets the
    configured header injected with the resolved token before httpx is
    called. The static smoke_test's ``headers`` are preserved alongside.
    """

    def test_exec_provider_injects_authorization_header(self, tmp_path):
        import httpx

        from fraisier.deployers.api import APIDeployer

        # Real on-disk script the provider calls — keeps subprocess
        # plumbing honest and the secret out of argv.
        token_file = tmp_path / "tok.txt"
        token_file.write_text("fresh-jwt-from-script")

        config = {
            "fraise_name": "myapi",
            "environment": "production",
            "app_path": "/srv/myapi",
            "clone_url": "git@github.com:org/myapi.git",
            "branch": "main",
            "systemd_service": "myapi.service",
            "health_check": {
                "url": "https://api.example.com/health",
                "timeout": 5,
            },
            "repos_base": "/tmp/repos",
            "smoke_tests": [
                {
                    "name": "auth",
                    "url": "/me",
                    "headers": {"X-Tenant": "abc"},
                    "token_provider": {
                        "type": "exec",
                        "command": ["cat", str(token_file)],
                    },
                    "assert": [],
                }
            ],
        }
        deployer = APIDeployer(config)

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        original_client = httpx.Client

        def make_client(*args, **kwargs):
            kwargs["transport"] = transport
            return original_client(*args, **kwargs)

        from unittest.mock import patch

        with (
            patch("fraisier.deployers.mixins.clone_bare_repo"),
            patch(
                "fraisier.deployers.mixins.fetch_and_checkout",
                return_value=("old", "new"),
            ),
            patch.object(deployer, "_restart_service"),
            patch.object(deployer, "_wait_for_health", return_value=True),
            patch("fraisier.smoke_tests.httpx.Client", side_effect=make_client),
        ):
            result = deployer.execute()

        assert result.success is True
        assert len(captured) == 1
        req = captured[0]
        assert req.headers["Authorization"] == "Bearer fresh-jwt-from-script"
        # Static header preserved alongside.
        assert req.headers["X-Tenant"] == "abc"


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

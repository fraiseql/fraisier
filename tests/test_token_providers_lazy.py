"""token_providers tolerate LazyEnv (#220).

Parser tolerance: the field-shape checks in ``token_providers`` accept
``LazyEnv`` placeholders for the secret-ish fields (``client_secret``
and friends) and propagate them. Only ``format`` is rejected because
the format string is code-shape, not config.

Consumer boundary: the OAuth2 ``resolve()`` path materializes
LazyEnv form-body fields to plain ``str`` before the httpx POST,
never relying on httpx's URL-encoder to call ``str()`` implicitly on
a placeholder. The form-body wire bytes are the audit surface — if a
LazyEnv leaked through, the request body would contain the
``LazyEnv(name=..., yaml_path=...)`` repr, not the secret.
"""

from __future__ import annotations

import httpx
import pytest

from fraisier.config._lazy_env import LazyEnv
from fraisier.errors import ConfigurationError
from fraisier.token_providers import (
    Oauth2ClientCredentialsTokenProvider,
    Oauth2RefreshTokenProvider,
    _require_str,
    _resolve_form_body,
    _validate_format,
)


class TestRequireStrAcceptsLazyEnv:
    def test_lazyenv_passes_without_resolving(self, monkeypatch):
        # X is intentionally unset — _require_str must not resolve.
        monkeypatch.delenv("X", raising=False)
        raw = {"client_secret": LazyEnv("X", "p")}
        value = _require_str(raw, "client_secret", "oauth2")
        assert isinstance(value, LazyEnv)
        assert value.name == "X"

    def test_str_still_works(self):
        raw = {"client_secret": "literal-secret"}
        assert _require_str(raw, "client_secret", "oauth2") == "literal-secret"

    def test_missing_still_raises(self):
        with pytest.raises(ConfigurationError, match=r"client_secret"):
            _require_str({}, "client_secret", "oauth2")

    def test_empty_str_still_raises(self):
        # LazyEnv is truthy by design, so the non-empty check survives.
        with pytest.raises(ConfigurationError, match=r"client_secret"):
            _require_str({"client_secret": ""}, "client_secret", "oauth2")

    def test_wrong_type_still_raises(self):
        with pytest.raises(ConfigurationError, match=r"client_secret"):
            _require_str({"client_secret": 42}, "client_secret", "oauth2")


class TestValidateFormatRejectsLazyEnv:
    def test_lazyenv_is_rejected(self):
        # format is code-shape ("Bearer {token}"), not config — !envvar
        # here is a foot-gun (the env var would have to contain a
        # well-formed format string, uncheckable at load).
        with pytest.raises(
            ConfigurationError,
            match=r"format must be a literal string, not !envvar",
        ):
            _validate_format(LazyEnv("FMT", "p"))

    def test_str_still_accepted(self):
        # Sanity: literal-string formats still parse fine.
        _validate_format("Bearer {token}")


def _install_mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "fraisier.token_providers._oauth2_http_transport",
        lambda: transport,
        raising=False,
    )


class TestResolveFormBody:
    def test_resolves_lazyenv_values(self, monkeypatch):
        monkeypatch.setenv("SECRET", "shh")
        body = _resolve_form_body(
            {
                "grant_type": "client_credentials",
                "client_secret": LazyEnv("SECRET", "p"),
                "scope": None,
            }
        )
        assert body == {
            "grant_type": "client_credentials",
            "client_secret": "shh",
            "scope": None,
        }

    def test_passes_str_through(self):
        body = _resolve_form_body({"a": "x", "b": None, "c": "y"})
        assert body == {"a": "x", "b": None, "c": "y"}

    def test_unset_lazy_surfaces_path(self, monkeypatch):
        monkeypatch.delenv("MISSING_SECRET", raising=False)
        with pytest.raises(
            ConfigurationError, match=r"MISSING_SECRET.*fraises\.api\.prod\.secret"
        ):
            _resolve_form_body(
                {"client_secret": LazyEnv("MISSING_SECRET", "fraises.api.prod.secret")}
            )


class TestOauth2ClientCredentialsResolveLazyForm:
    def test_form_body_carries_resolved_str_not_lazyenv_repr(self, monkeypatch):
        # The wire bytes of the POST body are the audit surface — if a
        # LazyEnv leaked through, the body would contain a `LazyEnv(...)`
        # repr instead of the resolved secret.
        monkeypatch.setenv("CSEC", "shh-resolved")
        monkeypatch.setenv("CID", "deploy-client")
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "abc"})

        _install_mock_transport(monkeypatch, handler)
        provider = Oauth2ClientCredentialsTokenProvider(
            token_url="https://idp.example.com/oauth/token",
            client_id=LazyEnv("CID", "fraises.api.token_provider.client_id"),
            client_secret=LazyEnv("CSEC", "fraises.api.token_provider.client_secret"),
        )
        assert provider.resolve() == "abc"
        body = captured["body"]
        assert "client_id=deploy-client" in body
        assert "client_secret=shh-resolved" in body
        assert "LazyEnv" not in body  # no placeholder leak

    def test_unset_client_secret_raises_with_path(self, monkeypatch):
        monkeypatch.delenv("CSEC", raising=False)

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("HTTP must not be issued when LazyEnv is unset")

        _install_mock_transport(monkeypatch, handler)
        provider = Oauth2ClientCredentialsTokenProvider(
            token_url="https://idp.example.com/oauth/token",
            client_id="cid",
            client_secret=LazyEnv("CSEC", "fraises.api.token_provider.client_secret"),
        )
        with pytest.raises(
            ConfigurationError,
            match=r"CSEC.*fraises\.api\.token_provider\.client_secret",
        ):
            provider.resolve()


class TestOauth2RefreshTokenResolveLazyForm:
    def test_form_body_carries_resolved_str_not_lazyenv_repr(self, monkeypatch):
        monkeypatch.setenv("RT", "rt-resolved")
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "abc"})

        _install_mock_transport(monkeypatch, handler)
        provider = Oauth2RefreshTokenProvider(
            token_url="https://idp.example.com/oauth/token",
            client_id="cid",
            refresh_token=LazyEnv("RT", "fraises.api.token_provider.refresh_token"),
        )
        assert provider.resolve() == "abc"
        body = captured["body"]
        assert "refresh_token=rt-resolved" in body
        assert "LazyEnv" not in body

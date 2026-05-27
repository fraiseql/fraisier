"""smoke_tests tolerates LazyEnv (#220).

Validator tolerance: parsers / schema checks accept ``LazyEnv``
placeholders for env-var-eligible fields and never call
``isinstance(_, str)`` directly on them.

Consumer-side materialization: ``materialize_test_headers`` is the
boundary that resolves LazyEnv values so httpx receives a concrete
``str`` — never relying on httpx's implicit ``str()``.

Logging-safety invariant: even if a LazyEnv slipped past
materialization, the smoke-test log line cannot leak the resolved
secret. Two defenses combine: ``_redacted_headers`` substitutes by
key name BEFORE touching the value, and ``%s``-style dict logging
routes through ``repr()`` on each value, hitting the non-resolving
``LazyEnv.__repr__``.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from fraisier.config import FraisierConfig
from fraisier.config._lazy_env import LazyEnv
from fraisier.errors import ConfigurationError
from fraisier.smoke_tests import (
    SmokeTest,
    _resolve_url,
    _run_one,
    load_smoke_tests,
    materialize_test_headers,
    resolve_test_url,
)


def _write_config(tmp_path, content):
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(content)
    return config_file


class TestResolveUrlAcceptsLazyEnv:
    def test_str_absolute_url_unchanged(self):
        assert _resolve_url("https://x/p", base_url=None) == "https://x/p"

    def test_lazyenv_resolves_to_absolute_url(self, monkeypatch):
        monkeypatch.setenv("SMOKE_URL", "https://x/p")
        assert _resolve_url(LazyEnv("SMOKE_URL", "p"), base_url=None) == "https://x/p"

    def test_lazyenv_resolves_to_relative_with_base(self, monkeypatch):
        monkeypatch.setenv("SMOKE_URL", "/graphql")
        assert (
            _resolve_url(LazyEnv("SMOKE_URL", "p"), base_url="https://api.x")
            == "https://api.x/graphql"
        )

    def test_unset_lazyenv_raises_at_resolve(self, monkeypatch):
        monkeypatch.delenv("SMOKE_URL", raising=False)
        with pytest.raises(ConfigurationError, match=r"SMOKE_URL.*not set"):
            _resolve_url(LazyEnv("SMOKE_URL", "p"), base_url=None)


class TestSmokeTestsLoadLazy:
    _TEMPLATE = """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        health_check:
          url: https://api.x/health
        smoke_tests:
          - url: {expr}
            method: GET
"""

    def test_unset_envvar_url_section_loads(self, tmp_path, monkeypatch):
        # Loading + Stage-2 validation must not resolve the URL.
        monkeypatch.delenv("SMOKE_URL", raising=False)
        config_file = _write_config(
            tmp_path, self._TEMPLATE.format(expr="!envvar SMOKE_URL")
        )
        config = FraisierConfig(config_file)
        # Stage-2 triggers smoke_tests validation, which now tolerates
        # LazyEnv in url:.
        fraise = config.get_fraise_environment("my_api", "production")
        assert fraise is not None
        raw = fraise["smoke_tests"][0]["url"]
        assert isinstance(raw, LazyEnv)
        assert raw.name == "SMOKE_URL"

    def test_load_smoke_tests_keeps_url_lazy(self, tmp_path, monkeypatch):
        # load_smoke_tests is the validator-level traversal; it must
        # not eagerly resolve. The stored SmokeTest.url is the raw
        # LazyEnv; resolve_test_url() finalizes at HTTP time.
        monkeypatch.delenv("SMOKE_URL", raising=False)
        config_file = _write_config(
            tmp_path, self._TEMPLATE.format(expr="!envvar SMOKE_URL")
        )
        config = FraisierConfig(config_file)
        fraise = config.get_fraise_environment("my_api", "production")
        assert fraise is not None
        tests = load_smoke_tests(fraise, base_url="https://api.x")
        assert isinstance(tests[0].url, LazyEnv)
        assert tests[0].base_url == "https://api.x"

    def test_resolve_test_url_runs_urljoin_at_http_time(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SMOKE_URL", "/graphql")
        config_file = _write_config(
            tmp_path, self._TEMPLATE.format(expr="!envvar SMOKE_URL")
        )
        config = FraisierConfig(config_file)
        fraise = config.get_fraise_environment("my_api", "production")
        assert fraise is not None
        tests = load_smoke_tests(fraise, base_url="https://api.x")
        assert resolve_test_url(tests[0]) == "https://api.x/graphql"

    def test_resolve_test_url_raises_when_envvar_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SMOKE_URL", raising=False)
        config_file = _write_config(
            tmp_path, self._TEMPLATE.format(expr="!envvar SMOKE_URL")
        )
        config = FraisierConfig(config_file)
        fraise = config.get_fraise_environment("my_api", "production")
        assert fraise is not None
        tests = load_smoke_tests(fraise, base_url="https://api.x")
        with pytest.raises(ConfigurationError, match=r"SMOKE_URL.*not set"):
            resolve_test_url(tests[0])


def _smoke_test_with_headers(headers: dict[str, object]) -> SmokeTest:
    """Construct a minimal SmokeTest with the given headers dict.

    SmokeTest's declared header type is ``dict[str, str]``, but at
    runtime ``LazyEnv`` values flow through here before
    materialization. The cast widens the test fixture without
    propagating the union into the production dataclass annotation.
    """
    return SmokeTest(
        name="t",
        method="GET",
        url="https://api.x/",
        headers=cast("dict[str, str]", headers),
        body=None,
        timeout=5,
        on_failure="halt",
        assertions=[],
        token_provider=None,
        base_url=None,
    )


class TestMaterializeTestHeaders:
    def test_headers_resolved_to_str(self, monkeypatch):
        # A LazyEnv header value must reach httpx as a concrete str —
        # materialize_test_headers is the boundary.
        monkeypatch.setenv("X_CUSTOM", "abc")
        t = _smoke_test_with_headers({"X-Custom": LazyEnv("X_CUSTOM", "p")})
        [materialized] = materialize_test_headers([t])
        assert materialized.headers == {"X-Custom": "abc"}
        assert isinstance(materialized.headers["X-Custom"], str)
        assert not isinstance(materialized.headers["X-Custom"], LazyEnv)

    def test_static_str_headers_pass_through(self):
        t = _smoke_test_with_headers({"X-Plain": "literal"})
        [materialized] = materialize_test_headers([t])
        assert materialized.headers == {"X-Plain": "literal"}

    def test_mixed_lazy_and_static_headers(self, monkeypatch):
        monkeypatch.setenv("TOK", "secret123")
        t = _smoke_test_with_headers(
            {
                "Authorization": LazyEnv("TOK", "p"),
                "X-Plain": "literal",
            }
        )
        [materialized] = materialize_test_headers([t])
        assert materialized.headers == {
            "Authorization": "secret123",
            "X-Plain": "literal",
        }

    def test_unset_lazy_header_raises_with_path(self, monkeypatch):
        # The error must surface from materialize_test_headers, not
        # later from httpx — and must carry the YAML path stamped by
        # the loader walker.
        monkeypatch.delenv("TOK", raising=False)
        t = _smoke_test_with_headers(
            {"Authorization": LazyEnv("TOK", "fraises.api.prod.smoke[0].headers.A")}
        )
        with pytest.raises(
            ConfigurationError,
            match=r"TOK.*fraises\.api\.prod\.smoke\[0\]\.headers\.A",
        ):
            materialize_test_headers([t])

    def test_token_provider_path_also_materializes_static(self, monkeypatch):
        # When a token_provider injects its own header, the existing
        # static headers must also be materialized — they were just
        # being passed through before.
        from fraisier.token_providers import ExecTokenProvider

        monkeypatch.setenv("EXTRA", "xvalue")

        # Subclass-with-overridden-resolve avoids spawning a subprocess
        # in this unit test; we only care that the static headers get
        # materialized alongside the injected provider header.
        class _StubProvider(ExecTokenProvider):
            def resolve(self) -> str:
                return "static-token"

        provider = _StubProvider(
            header="Authorization",
            format="Bearer {token}",
            command=("/bin/true",),
        )
        t = replace(
            _smoke_test_with_headers({"X-Extra": LazyEnv("EXTRA", "p")}),
            token_provider=provider,
        )
        [materialized] = materialize_test_headers([t])
        # Static LazyEnv header resolved AND provider header injected.
        assert materialized.headers["X-Extra"] == "xvalue"
        assert materialized.headers["Authorization"] == "Bearer static-token"
        assert all(isinstance(v, str) for v in materialized.headers.values())


def _stub_httpx_client():
    """Return an httpx.Client patch context that yields a 200/JSON response."""
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {}
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.request.return_value = mock_resp
    return mock_client


class TestLoggingSafetyForLazyHeaders:
    """Defense-in-depth: even if materialization is bypassed and a raw
    LazyEnv flows into ``_run_one``, the log line cannot leak the
    resolved secret.

    Two defenses combine:
      1. ``_redacted_headers`` substitutes auth-shaped values by key
         name BEFORE touching the value — for Authorization/Cookie/
         X-API-Key the value never gets converted to a string.
      2. The redacted dict is logged via ``%s`` formatting, which
         routes through ``repr()`` on each value. ``LazyEnv.__repr__``
         does NOT resolve, so non-redacted LazyEnvs render as
         ``LazyEnv(name='X', yaml_path='Y')`` placeholders — never
         the resolved secret.
    """

    def test_logging_redacts_lazy_authorization(self, monkeypatch, caplog):
        # Authorization is redacted by key name; even though TOK is set,
        # the resolved value must NEVER appear in the log line.
        monkeypatch.setenv("TOK", "topsecret")
        t = _smoke_test_with_headers({"Authorization": LazyEnv("TOK", "p")})
        with (
            patch(
                "fraisier.smoke_tests.httpx.Client",
                return_value=_stub_httpx_client(),
            ),
            caplog.at_level(logging.INFO, logger="fraisier.smoke_tests"),
        ):
            _run_one(t)
        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "topsecret" not in joined
        assert "***redacted***" in joined

    def test_logging_does_not_resolve_non_redacted_lazy_value(
        self, monkeypatch, caplog
    ):
        # Non-redacted header: the LazyEnv is logged via dict repr →
        # safe placeholder, never the resolved "customvalue".
        monkeypatch.setenv("CUSTOM", "customvalue")
        t = _smoke_test_with_headers({"X-Custom": LazyEnv("CUSTOM", "p")})
        with (
            patch(
                "fraisier.smoke_tests.httpx.Client",
                return_value=_stub_httpx_client(),
            ),
            caplog.at_level(logging.INFO, logger="fraisier.smoke_tests"),
        ):
            _run_one(t)
        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "customvalue" not in joined
        # Repr-based placeholder is what the dict-format produces.
        assert "LazyEnv(name='CUSTOM'" in joined

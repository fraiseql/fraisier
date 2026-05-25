"""Phase 3 — smoke_tests tolerates LazyEnv (#220).

Phase 5 Cycle 5.1 adds explicit header materialization at the
materialize_test_headers boundary so the value type reaching httpx is
a concrete ``str`` and never relies on httpx's implicit ``str()``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from fraisier.config import FraisierConfig
from fraisier.config._lazy_env import LazyEnv
from fraisier.errors import ConfigurationError
from fraisier.smoke_tests import (
    SmokeTest,
    _resolve_url,
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
        tests = load_smoke_tests(fraise, base_url="https://api.x")
        assert resolve_test_url(tests[0]) == "https://api.x/graphql"

    def test_resolve_test_url_raises_when_envvar_missing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SMOKE_URL", raising=False)
        config_file = _write_config(
            tmp_path, self._TEMPLATE.format(expr="!envvar SMOKE_URL")
        )
        config = FraisierConfig(config_file)
        fraise = config.get_fraise_environment("my_api", "production")
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
        # later from httpx — and must carry the YAML path (Phase 4).
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

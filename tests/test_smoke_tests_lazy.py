"""Phase 3 — smoke_tests tolerates LazyEnv (#220)."""

from __future__ import annotations

import pytest

from fraisier.config import FraisierConfig
from fraisier.config._lazy_env import LazyEnv
from fraisier.errors import ConfigurationError
from fraisier.smoke_tests import _resolve_url, load_smoke_tests, resolve_test_url


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

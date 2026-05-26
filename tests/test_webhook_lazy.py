"""Webhook consumer audit — LazyEnv in provider config (#220).

The git provider config block (``git.github`` / ``git.gitlab`` / etc.)
typically carries secrets — webhook_secret, API tokens, app keys — and
these are prime candidates for ``!envvar`` references in fraises.yaml.
The provider constructors expect ``str`` for these fields and call
``.encode()`` on them; a raw ``LazyEnv`` would raise ``AttributeError``.

The audit boundary is the provider-config dict spread in
``get_git_provider`` and ``_verify_signature``. Both sites now route
the raw config dict through ``_resolve_provider_config`` so every
``LazyEnv`` value is materialized to ``str`` before the provider sees
it.
"""

from __future__ import annotations

import pytest

from fraisier.config._lazy_env import LazyEnv
from fraisier.errors import ConfigurationError
from fraisier.webhook import _resolve_provider_config


class TestResolveProviderConfig:
    def test_passes_plain_dict_through(self):
        raw = {"webhook_secret": "x" * 40, "base_url": "https://gh.example.com"}
        assert _resolve_provider_config(raw) == raw

    def test_materializes_lazyenv_values(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "ghs_resolved")
        monkeypatch.setenv("GH_WHS", "wh-resolved-" + "x" * 28)
        out = _resolve_provider_config(
            {
                "token": LazyEnv("GH_TOKEN", "fraises.api.git.github.token"),
                "webhook_secret": LazyEnv("GH_WHS", "fraises.api.git.github.whs"),
                "base_url": "https://gh.example.com",
            }
        )
        assert out["token"] == "ghs_resolved"
        assert out["webhook_secret"] == "wh-resolved-" + "x" * 28
        assert out["base_url"] == "https://gh.example.com"
        for k, v in out.items():
            assert not isinstance(v, LazyEnv), f"leftover LazyEnv in {k!r}"

    def test_non_string_values_pass_through(self):
        # Booleans / ints / nested mappings stay as-is.
        raw = {
            "webhook_secret": "x" * 40,
            "verify_tls": True,
            "rate_limit": 100,
            "retries": {"max": 3, "backoff_ms": 250},
        }
        assert _resolve_provider_config(raw) == raw

    def test_none_values_pass_through(self):
        # When a key is explicitly None (e.g. webhook_secret unset),
        # it survives — provider does its own None check.
        out = _resolve_provider_config({"webhook_secret": None})
        assert out == {"webhook_secret": None}

    def test_unset_lazyenv_raises_with_path(self, monkeypatch):
        monkeypatch.delenv("GH_TOKEN", raising=False)
        with pytest.raises(
            ConfigurationError,
            match=r"GH_TOKEN.*fraises\.api\.git\.github\.token",
        ):
            _resolve_provider_config(
                {"token": LazyEnv("GH_TOKEN", "fraises.api.git.github.token")}
            )


class TestGetGitProviderResolvesLazySecret:
    def test_provider_receives_plain_str_webhook_secret(self, monkeypatch, tmp_path):
        # End-to-end: fraises.yaml with `git.github.webhook_secret:
        # !envvar GH_WHS`. The resolved provider must hold a plain str
        # so it can call `.encode()` during signature verification.
        from fraisier import webhook as webhook_mod
        from fraisier.config import FraisierConfig
        from fraisier.config import loader as config_loader

        whs = "z" * 40
        monkeypatch.setenv("GH_WHS", whs)

        cfg_file = tmp_path / "fraises.yaml"
        cfg_file.write_text(
            """
git:
  provider: github
  github:
    webhook_secret: !envvar GH_WHS
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
"""
        )
        # Inject the just-built config as the global singleton so
        # `get_git_provider`'s `get_config()` returns it.
        monkeypatch.setattr(config_loader, "_config", FraisierConfig(cfg_file))
        monkeypatch.delenv("FRAISIER_WEBHOOK_SECRET", raising=False)
        monkeypatch.delenv("FRAISIER_GIT_PROVIDER", raising=False)
        monkeypatch.delenv("FRAISIER_GIT_URL", raising=False)

        provider = webhook_mod.get_git_provider()
        # Provider holds the resolved string, not a LazyEnv.
        assert provider.webhook_secret == whs
        assert isinstance(provider.webhook_secret, str)

"""Phase 3 — token_providers tolerate LazyEnv (#220).

The field-shape checks in ``token_providers`` historically required
plain ``str`` for ``client_secret`` and friends, which rejected
``LazyEnv`` placeholders. After Phase 3 the secret-ish fields accept
``LazyEnv`` and propagate it; only ``format`` is rejected because the
format string is code-shape, not config.
"""

from __future__ import annotations

import pytest

from fraisier.config._lazy_env import LazyEnv
from fraisier.errors import ConfigurationError
from fraisier.token_providers import _require_str, _validate_format


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

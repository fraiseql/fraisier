"""Tests for the LazyEnv placeholder and to_str() helper (#220 Phase 2).

LazyEnv defers `os.environ[NAME]` resolution from YAML load time to the
boundary where a consumer (or validator) actually inspects the value.

Resolution is read-each-access: every call to ``resolve()`` /
``to_str(x)`` performs a fresh ``os.environ`` lookup with no cache.
"""

from __future__ import annotations

import pickle

import pytest

from fraisier.config._lazy_env import LazyEnv, to_str
from fraisier.errors import ConfigurationError


class TestLazyEnvCore:
    def test_no_resolve_until_str(self, monkeypatch):
        monkeypatch.delenv("FOO", raising=False)
        env = LazyEnv("FOO", "x.y")
        # Construction with unset var must not raise.
        assert env.name == "FOO"
        assert env.yaml_path == "x.y"
        with pytest.raises(ConfigurationError, match=r"FOO.*x\.y"):
            env.resolve()

    def test_resolve_returns_env_value(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        env = LazyEnv("FOO", "x.y")
        assert env.resolve() == "bar"

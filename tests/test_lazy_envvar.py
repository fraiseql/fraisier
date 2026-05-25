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


class TestLazyEnvStrParity:
    def test_str_returns_resolved_value(self, monkeypatch):
        monkeypatch.setenv("V", "abc")
        x = LazyEnv("V", "p")
        assert str(x) == "abc"

    def test_format_returns_resolved_value(self, monkeypatch):
        monkeypatch.setenv("V", "abc")
        x = LazyEnv("V", "p")
        assert f"{x}" == "abc"

    def test_fspath_returns_resolved_value(self, monkeypatch):
        import os

        monkeypatch.setenv("V", "abc")
        x = LazyEnv("V", "p")
        assert os.fspath(x) == "abc"

    def test_concat_via_str(self, monkeypatch):
        monkeypatch.setenv("V", "abc")
        x = LazyEnv("V", "p")
        assert "prefix-" + str(x) == "prefix-abc"

    def test_eq_against_str_true_and_false(self, monkeypatch):
        monkeypatch.setenv("V", "abc")
        x = LazyEnv("V", "p")
        assert x == "abc"
        assert not (x == "xyz")  # noqa: SIM201 — assert __eq__ returns False directly
        assert x != "xyz"

    def test_eq_reflected_from_str(self, monkeypatch):
        # str.__eq__(LazyEnv) returns NotImplemented; Python falls
        # back to LazyEnv.__eq__(str). The reflected equality must
        # still resolve to True.
        monkeypatch.setenv("V", "abc")
        x = LazyEnv("V", "p")
        assert "abc" == x  # noqa: SIM300 — testing reflected equality

    def test_eq_lazyenv_to_lazyenv(self, monkeypatch):
        monkeypatch.setenv("V", "abc")
        monkeypatch.setenv("W", "abc")
        monkeypatch.setenv("Z", "def")
        assert LazyEnv("V", "p") == LazyEnv("W", "q")
        assert LazyEnv("V", "p") != LazyEnv("Z", "q")

    def test_hash_matches_str(self, monkeypatch):
        monkeypatch.setenv("V", "abc")
        x = LazyEnv("V", "p")
        assert hash(x) == hash("abc")

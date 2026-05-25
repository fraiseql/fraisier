"""Tests for the LazyEnv placeholder and to_str() helper (#220 Phase 2).

LazyEnv defers `os.environ[NAME]` resolution from YAML load time to the
boundary where a consumer (or validator) actually inspects the value.

Resolution is read-each-access: every call to ``resolve()`` /
``to_str(x)`` performs a fresh ``os.environ`` lookup with no cache.
"""

from __future__ import annotations

import pickle

import pytest

from fraisier.config._lazy_env import LazyEnv, is_string_like, to_str
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


class TestLazyEnvSafety:
    def test_repr_does_not_resolve_when_unset(self, monkeypatch):
        monkeypatch.delenv("V", raising=False)
        x = LazyEnv("V", "p")
        r = repr(x)
        assert "V" in r
        assert "p" in r

    def test_repr_does_not_leak_value_when_set(self, monkeypatch):
        monkeypatch.setenv("V", "TOPSECRET")
        x = LazyEnv("V", "p")
        assert "TOPSECRET" not in repr(x)

    def test_repr_is_deterministic(self, monkeypatch):
        monkeypatch.delenv("V", raising=False)
        x = LazyEnv("V", "a.b")
        assert repr(x) == "LazyEnv(name='V', yaml_path='a.b')"

    def test_bool_is_true_without_resolving(self, monkeypatch):
        monkeypatch.delenv("V", raising=False)
        x = LazyEnv("V", "p")
        # No ConfigurationError despite V being unset.
        assert bool(x) is True

    def test_pickle_round_trip(self, monkeypatch):
        monkeypatch.delenv("V", raising=False)
        x = LazyEnv("V", "p")
        y = pickle.loads(pickle.dumps(x))
        assert isinstance(y, LazyEnv)
        assert y.name == "V"
        assert y.yaml_path == "p"

    def test_ordering_is_typeerror(self):
        with pytest.raises(TypeError):
            _ = LazyEnv("V", "p") < LazyEnv("W", "p")

    def test_containment_is_typeerror(self):
        with pytest.raises(TypeError):
            _ = "a" in LazyEnv("V", "p")

    def test_iter_is_typeerror(self):
        with pytest.raises(TypeError):
            iter(LazyEnv("V", "p"))


class TestToStr:
    def test_passes_str_through(self):
        assert to_str("foo") == "foo"

    def test_resolves_lazyenv(self, monkeypatch):
        monkeypatch.setenv("V", "bar")
        assert to_str(LazyEnv("V", "p")) == "bar"

    def test_raises_on_unset_lazyenv(self, monkeypatch):
        monkeypatch.delenv("V", raising=False)
        with pytest.raises(ConfigurationError, match=r"V.*not set"):
            to_str(LazyEnv("V", "p"))

    def test_reexported_from_config_package(self):
        # The boundary helper is part of fraisier.config's public surface
        # so consumers can import it without reaching into _lazy_env.
        from fraisier.config import LazyEnv as PkgLazyEnv
        from fraisier.config import to_str as pkg_to_str

        assert pkg_to_str is to_str
        assert PkgLazyEnv is LazyEnv


class TestIsStringLike:
    def test_str_is_string_like(self):
        assert is_string_like("foo") is True

    def test_lazyenv_is_string_like(self):
        # No resolve() call — check is purely structural.
        assert is_string_like(LazyEnv("FOO_UNSET", "p")) is True

    def test_empty_str_is_string_like(self):
        # Type widening doesn't add a truthy requirement.
        assert is_string_like("") is True

    def test_int_is_not_string_like(self):
        assert is_string_like(42) is False

    def test_none_is_not_string_like(self):
        assert is_string_like(None) is False

    def test_bytes_is_not_string_like(self):
        # bytes deliberately excluded — too easy to confuse with str.
        assert is_string_like(b"foo") is False

    def test_list_is_not_string_like(self):
        assert is_string_like(["foo"]) is False


class TestPathFallback:
    def test_resolve_with_none_path(self, monkeypatch):
        # A LazyEnv constructed outside the loader (e.g. directly in
        # tests) may have yaml_path=None. resolve() must not crash on
        # NoneType and must surface "<unknown>" instead.
        monkeypatch.delenv("V", raising=False)
        x = LazyEnv("V", yaml_path=None)
        with pytest.raises(ConfigurationError, match=r"V.*<unknown>"):
            x.resolve()

    def test_resolve_with_empty_path(self, monkeypatch):
        monkeypatch.delenv("V", raising=False)
        x = LazyEnv("V", yaml_path="")
        with pytest.raises(ConfigurationError, match=r"V.*<unknown>"):
            x.resolve()


class TestReadEachAccess:
    def test_resolve_reads_each_call(self, monkeypatch):
        # The contract: no caching. Each to_str() / resolve() consults
        # os.environ fresh, so mid-process env mutations are observed.
        env = LazyEnv("V", "p")
        monkeypatch.setenv("V", "x")
        assert to_str(env) == "x"
        monkeypatch.setenv("V", "y")
        assert to_str(env) == "y"
        # And via .resolve() directly.
        monkeypatch.setenv("V", "z")
        assert env.resolve() == "z"

"""Tests for the !envvar YAML tag (#204 PR B).

Allows secrets to be sourced from the environment at load time rather
than embedded in the on-disk YAML. Use:

    headers:
      Authorization: !envvar SMOKE_TEST_JWT

The tag resolves to the string contents of ``os.environ['SMOKE_TEST_JWT']``.
Missing variables raise ``ConfigurationError`` at load time so the
misconfig is visible immediately rather than at deploy time when the
secret is needed.
"""

from __future__ import annotations

import pytest

from fraisier.config import FraisierConfig, LazyEnv, to_str
from fraisier.errors import ConfigurationError


def _write_config(tmp_path, content):
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(content)
    return config_file


_TEMPLATE = """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        secret: {expr}
"""


class TestEnvvarYamlTag:
    def test_resolves_string_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SMOKE_TEST_JWT", "eyJsuper-secret")
        config_file = _write_config(
            tmp_path, _TEMPLATE.format(expr="!envvar SMOKE_TEST_JWT")
        )
        config = FraisierConfig(config_file)
        fraise = config.get_fraise_environment("my_api", "production")
        assert fraise is not None
        assert fraise["secret"] == "eyJsuper-secret"

    def test_raises_when_env_var_missing(self, tmp_path, monkeypatch):
        # Load does not raise; the error surfaces at the boundary
        # where a consumer coerces the LazyEnv to a str.
        monkeypatch.delenv("MISSING_VAR", raising=False)
        config_file = _write_config(
            tmp_path, _TEMPLATE.format(expr="!envvar MISSING_VAR")
        )
        config = FraisierConfig(config_file)
        fraise = config.get_fraise_environment("my_api", "production")
        assert fraise is not None
        with pytest.raises(ConfigurationError, match=r"MISSING_VAR.*not set"):
            to_str(fraise["secret"])

    def test_unset_envvar_loads_without_raise(self, tmp_path, monkeypatch):
        # ``!envvar`` resolution is deferred to consumption. Loading a
        # YAML that references an unset env var no longer raises at
        # parse time — the value materializes as a LazyEnv.
        monkeypatch.delenv("MISSING_VAR", raising=False)
        config_file = _write_config(
            tmp_path, _TEMPLATE.format(expr="!envvar MISSING_VAR")
        )
        config = FraisierConfig(config_file)
        fraise = config.get_fraise_environment("my_api", "production")
        assert fraise is not None
        assert isinstance(fraise["secret"], LazyEnv)
        assert fraise["secret"].name == "MISSING_VAR"

    def test_empty_string_env_var_resolves(self, tmp_path, monkeypatch):
        # An empty string is still a "set" env var; resolves to "".
        monkeypatch.setenv("EMPTY_VAR", "")
        config_file = _write_config(
            tmp_path, _TEMPLATE.format(expr="!envvar EMPTY_VAR")
        )
        config = FraisierConfig(config_file)
        fraise = config.get_fraise_environment("my_api", "production")
        assert fraise is not None
        assert fraise["secret"] == ""

    def test_works_inside_nested_structures(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOK", "abc")
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        secrets:
          - !envvar TOK
          - literal_value
""",
        )
        config = FraisierConfig(config_file)
        fraise = config.get_fraise_environment("my_api", "production")
        assert fraise is not None
        assert fraise["secrets"] == ["abc", "literal_value"]

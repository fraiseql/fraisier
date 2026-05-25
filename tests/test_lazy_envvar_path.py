"""Tests for LazyEnv.yaml_path attached by the loader walker (#220 Phase 4).

The loader walks the parsed YAML tree and stamps every ``LazyEnv``
placeholder with the dotted-indexed path where it was found. Resolution
errors then point operators at the exact YAML key, instead of the
"<unknown>" placeholder used at construction time.
"""

from __future__ import annotations

import pytest

from fraisier.config import FraisierConfig, to_str
from fraisier.errors import ConfigurationError


def _write_config(tmp_path, content):
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(content)
    return config_file


class TestPathFromSequence:
    def test_path_in_sequence(self, tmp_path, monkeypatch):
        monkeypatch.delenv("A", raising=False)
        monkeypatch.delenv("B", raising=False)
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
          - !envvar A
          - !envvar B
""",
        )
        config = FraisierConfig(config_file)
        env = config.get_fraise_environment("my_api", "production")
        assert env is not None
        with pytest.raises(
            ConfigurationError,
            match=(r"fraises\.my_api\.environments\.production\.secrets\[0\]"),
        ):
            to_str(env["secrets"][0])
        with pytest.raises(
            ConfigurationError,
            match=(r"fraises\.my_api\.environments\.production\.secrets\[1\]"),
        ):
            to_str(env["secrets"][1])


class TestPathDeepNesting:
    def test_full_smoke_test_path(self, tmp_path, monkeypatch):
        # Full reproduction from issue #220: an !envvar lives under a
        # mapping under a list under a mapping under the smoke_tests
        # block. The walker has to recurse through all four layers and
        # land on the exact dotted-indexed path.
        monkeypatch.delenv("SMOKE_TEST_JWT", raising=False)
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        smoke_tests:
          - name: auth
            url: https://example.com/me
            headers:
              Authorization: !envvar SMOKE_TEST_JWT
""",
        )
        config = FraisierConfig(config_file)
        env = config.get_fraise_environment("my_api", "production")
        assert env is not None
        with pytest.raises(
            ConfigurationError,
            match=(
                r"fraises\.my_api\.environments\.production"
                r"\.smoke_tests\[0\]\.headers\.Authorization"
            ),
        ):
            to_str(env["smoke_tests"][0]["headers"]["Authorization"])


class TestPathFromMapping:
    def test_path_in_nested_mapping(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MISSING", raising=False)
        config_file = _write_config(
            tmp_path,
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        secret: !envvar MISSING
""",
        )
        config = FraisierConfig(config_file)
        env = config.get_fraise_environment("my_api", "production")
        assert env is not None
        with pytest.raises(
            ConfigurationError,
            match=r"fraises\.my_api\.environments\.production\.secret",
        ):
            to_str(env["secret"])

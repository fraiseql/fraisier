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

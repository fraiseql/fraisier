"""Tests for the two-stage validation split.

Phase 1 of issue #220 — Stage 1 runs in ``FraisierConfig.__init__`` and is
cheap (shape only). Stage 2 (deep validation per section) runs on first
access of the matching property.
"""

import pytest

from fraisier.config import FraisierConfig
from fraisier.errors import ValidationError


def _write(tmp_path, content):
    path = tmp_path / "fraises.yaml"
    path.write_text(content)
    return path


class TestStage1Budget:
    """Stage 1 must not trigger deep validators."""

    def test_init_does_not_validate_notifications(self, tmp_path):
        """A structurally-broken notifications block must not break __init__."""
        cfg = _write(
            tmp_path,
            """
git:
  provider: github
notifications:
  on_failure:
    - type: fax_machine
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""",
        )
        # Stage 1: must NOT raise — notifications is deep-validated lazily.
        config = FraisierConfig(str(cfg))

        # Stage 2: first access surfaces the validation error.
        with pytest.raises(ValidationError, match="fax_machine"):
            _ = config.notifications

    def test_init_does_not_validate_fraises(self, tmp_path):
        """A fraise with an invalid env must not break __init__."""
        cfg = _write(
            tmp_path,
            """
git:
  provider: github
fraises:
  good:
    type: api
    environments:
      prod:
        app_path: /tmp/good
  bad:
    type: api
    environments:
      prod:
        app_path: /tmp/bad
        health_check:
          timeout: "not-a-number"
""",
        )
        # Stage 1: must NOT raise — fraise envs are deep-validated lazily.
        config = FraisierConfig(str(cfg))

        # Naming-only operations stay cheap (no Stage-2 validation).
        assert set(config.list_fraises()) == {"good", "bad"}
        deployments = config.list_all_deployments()
        assert {d["fraise"] for d in deployments} == {"good", "bad"}
        detailed = config.list_fraises_detailed()
        assert {f["name"] for f in detailed} == {"good", "bad"}
        # list_environments only reads names, never validates.
        assert config.list_environments("bad") == ["prod"]

        # Stage 2: per-env access surfaces the validation error.
        assert config.get_fraise_environment("good", "prod") is not None
        with pytest.raises(ValidationError, match="timeout"):
            config.get_fraise_environment("bad", "prod")

    def test_fraise_env_validation_is_memoized(self, tmp_path):
        """Repeated access to the same (fraise, env) validates exactly once."""
        cfg = _write(
            tmp_path,
            """
git:
  provider: github
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""",
        )
        config = FraisierConfig(str(cfg))
        # First call validates, second is cached.
        first = config.get_fraise_environment("api", "prod")
        second = config.get_fraise_environment("api", "prod")
        assert first == second

    def test_valid_notifications_is_memoized(self, tmp_path):
        """Repeated access validates exactly once."""
        cfg = _write(
            tmp_path,
            """
git:
  provider: github
notifications:
  on_failure:
    - type: slack
      webhook_url: https://hooks.example.com/abc
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""",
        )
        config = FraisierConfig(str(cfg))
        # Access twice — should be cached.
        first = config.notifications
        second = config.notifications
        assert first is second

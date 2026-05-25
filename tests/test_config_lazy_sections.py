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

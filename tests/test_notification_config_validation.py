"""Tests for notifications config validation."""

import pytest

from fraisier.config import FraisierConfig
from fraisier.errors import ValidationError


class TestNotificationConfigValidation:
    def test_valid_slack_config(self, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
notifications:
  on_failure:
    - type: slack
      webhook_url: https://hooks.slack.com/xxx
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        config = FraisierConfig(str(cfg))
        assert config.notifications is not None

    def test_invalid_notifier_type(self, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
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
""")
        with pytest.raises(ValidationError, match="fax_machine"):
            FraisierConfig(str(cfg))

    def test_slack_missing_webhook_url(self, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
notifications:
  on_failure:
    - type: slack
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        with pytest.raises(ValidationError, match="webhook_url"):
            FraisierConfig(str(cfg))

    def test_webhook_missing_url(self, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
notifications:
  on_success:
    - type: webhook
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        with pytest.raises(ValidationError, match="url"):
            FraisierConfig(str(cfg))

    def test_github_issue_missing_repo(self, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
notifications:
  on_failure:
    - type: github_issue
      token: ghp_xxx
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        with pytest.raises(ValidationError, match="repo"):
            FraisierConfig(str(cfg))

    def test_no_notifications_section_is_valid(self, tmp_path):
        cfg = tmp_path / "fraises.yaml"
        cfg.write_text("""
git:
  provider: github
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
""")
        config = FraisierConfig(str(cfg))
        assert config.notifications == {}

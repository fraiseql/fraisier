"""Tests for configurable lock_timeout per fraise environment."""

from unittest.mock import MagicMock

from fraisier.config import FraisierConfig
from fraisier.deployers.api import APIDeployer


class TestLockTimeoutConfig:
    """Verify lock_timeout is parsed from fraises.yaml and flows to deployers."""

    def test_lock_timeout_parsed_from_env_config(self, tmp_path):
        """lock_timeout in fraises.yaml environment should be accessible."""
        cfg_file = tmp_path / "fraises.yaml"
        cfg_file.write_text("""
git:
  provider: github
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /tmp/api
        lock_timeout: 120
""")
        config = FraisierConfig(str(cfg_file))
        env = config.get_fraise_environment("my_api", "production")
        assert env is not None
        assert env["lock_timeout"] == 120

    def test_lock_timeout_defaults_to_300(self, tmp_path):
        """Missing lock_timeout should default to 300 in deployer."""
        cfg_file = tmp_path / "fraises.yaml"
        cfg_file.write_text("""
git:
  provider: github
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /tmp/api
""")
        config = FraisierConfig(str(cfg_file))
        env = config.get_fraise_environment("my_api", "production")
        deployer = APIDeployer(env, runner=MagicMock())
        assert deployer.lock_timeout == 300

    def test_lock_timeout_flows_to_deployer(self, tmp_path):
        """lock_timeout from config should be stored on deployer."""
        cfg_file = tmp_path / "fraises.yaml"
        cfg_file.write_text("""
git:
  provider: github
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /tmp/api
        lock_timeout: 60
""")
        config = FraisierConfig(str(cfg_file))
        env = config.get_fraise_environment("my_api", "production")
        deployer = APIDeployer(env, runner=MagicMock())
        assert deployer.lock_timeout == 60

    def test_lock_timeout_validation_rejects_non_numeric(self, tmp_path):
        """Non-numeric lock_timeout should fail config validation."""
        import pytest

        from fraisier.errors import ValidationError

        cfg_file = tmp_path / "fraises.yaml"
        cfg_file.write_text("""
git:
  provider: github
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /tmp/api
        lock_timeout: "not_a_number"
""")
        with pytest.raises(ValidationError, match="lock_timeout"):
            FraisierConfig(str(cfg_file))

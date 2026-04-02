"""Tests for framework configuration system."""

import os

from fraisier.framework_config import FrameworkConfig


class TestFrameworkConfig:
    """Test framework configuration loading with precedence."""

    def test_config_precedence_flags_over_user(self, tmp_path):
        """Flags override user config."""
        # Create user config
        user_config = tmp_path / "user.toml"
        user_config.write_text("""
[logging]
level = "INFO"
""")

        # Load with flag override
        config = FrameworkConfig.load(
            user_config_path=user_config, overrides={"logging.level": "DEBUG"}
        )

        assert config.get("logging.level") == "DEBUG"

    def test_config_precedence_user_over_system(self, tmp_path):
        """User config overrides system config."""
        # Create system config
        system_config = tmp_path / "system.toml"
        system_config.write_text("""
[logging]
level = "INFO"
""")

        # Create user config
        user_config = tmp_path / "user.toml"
        user_config.write_text("""
[logging]
level = "DEBUG"
""")

        config = FrameworkConfig.load(
            system_config_path=system_config, user_config_path=user_config
        )

        assert config.get("logging.level") == "DEBUG"

    def test_config_precedence_system_over_defaults(self, tmp_path):
        """System config overrides defaults."""
        # Create system config
        system_config = tmp_path / "system.toml"
        system_config.write_text("""
[logging]
level = "DEBUG"
""")

        config = FrameworkConfig.load(system_config_path=system_config)

        assert config.get("logging.level") == "DEBUG"

    def test_config_env_override(self, tmp_path, monkeypatch):
        """Environment variables override config files."""
        # Create config file
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[logging]
level = "INFO"
""")

        # Set env var
        monkeypatch.setenv("FRAISIER_LOGGING__LEVEL", "DEBUG")

        config = FrameworkConfig.load(user_config_path=config_file)

        assert config.get("logging.level") == "DEBUG"

    def test_config_defaults(self, monkeypatch):
        """Default values are used when no config provided."""
        # Clear any FRAISIER env vars that might interfere
        for key in list(os.environ.keys()):
            if key.startswith("FRAISIER_"):
                monkeypatch.delenv(key, raising=False)

        config = FrameworkConfig.load()

        assert config.get("logging.level") == "DEBUG"  # Default
        assert config.get("timeout") == 300  # Assume default

    def test_setup_logging_from_config(self):
        """Test logging setup from framework config."""
        from fraisier.logging import JSONFormatter, setup_logging_from_config

        # Test with JSON format
        config = FrameworkConfig({"logging": {"level": "DEBUG", "format": "json"}})
        logger = setup_logging_from_config(config)

        assert logger.level == 10  # DEBUG
        # Check that handler has JSON formatter
        handler = logger.handlers[0]
        assert isinstance(handler.formatter, JSONFormatter)

        # Test with text format
        config = FrameworkConfig({"logging": {"level": "INFO", "format": "text"}})
        logger = setup_logging_from_config(config)

        assert logger.level == 20  # INFO
        handler = logger.handlers[0]
        # Text formatter has a format string
        assert hasattr(handler.formatter, "_fmt")

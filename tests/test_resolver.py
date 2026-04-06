"""Tests for ConfigResolver — unified environment variable resolution."""

from pathlib import Path

import pytest

from fraisier.config import ConfigurationError
from fraisier.resolver import ConfigResolver


class TestDbPath:
    """ConfigResolver.db_path: resolve FRAISIER_DB_PATH with fallbacks."""

    def test_returns_path_object(self):
        """db_path always returns a Path object, never a string."""
        resolver = ConfigResolver(environ={})
        assert isinstance(resolver.db_path, Path)

    def test_env_var_takes_priority(self):
        """FRAISIER_DB_PATH env var overrides all defaults."""
        environ = {"FRAISIER_DB_PATH": "/custom/db.db"}
        resolver = ConfigResolver(environ=environ)
        assert resolver.db_path == Path("/custom/db.db")

    def test_default_when_env_unset(self, tmp_path, monkeypatch):
        """When unset, returns /var/lib/fraisier/fraisier.db if it exists."""
        monkeypatch.setattr(
            "fraisier.resolver.DEFAULT_DB_PATH", tmp_path / "fraisier.db"
        )
        tmp_path.mkdir(exist_ok=True)

        resolver = ConfigResolver(environ={})
        assert resolver.db_path == tmp_path / "fraisier.db"

    def test_fallback_when_default_missing(self):
        """When /var/lib/fraisier/ doesn't exist, fallback to package dir."""
        resolver = ConfigResolver(environ={})
        # This should return a path in the package directory
        assert resolver.db_path.name == "fraisier.db"
        assert "fraisier" in str(resolver.db_path)


class TestConfigPath:
    """ConfigResolver.config_path: resolve FRAISIER_CONFIG with search locations."""

    def test_env_var_takes_priority(self, tmp_path):
        """FRAISIER_CONFIG env var overrides search locations."""
        config_file = tmp_path / "custom.yaml"
        config_file.write_text("test")

        environ = {"FRAISIER_CONFIG": str(config_file)}
        resolver = ConfigResolver(environ=environ)
        assert resolver.config_path == config_file

    def test_searches_locations_when_unset(self, tmp_path, monkeypatch):
        """When unset, searches standard locations in order."""
        # Create a config file in one of the search locations
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text("test config")

        # Mock _config_search_locations to include our temp path
        search_locs = [
            tmp_path / "nonexistent.yaml",
            tmp_path / "fraises.yaml",
        ]
        monkeypatch.setattr(
            "fraisier.resolver._config_search_locations", lambda: search_locs
        )

        resolver = ConfigResolver(environ={})
        assert resolver.config_path == config_file

    def test_raises_when_no_file_found(self, monkeypatch):
        """Raises ConfigurationError when no config file found."""
        monkeypatch.setattr(
            "fraisier.resolver._config_search_locations",
            lambda: [Path("/nonexistent/1.yaml"), Path("/nonexistent/2.yaml")],
        )

        resolver = ConfigResolver(environ={})
        with pytest.raises(ConfigurationError, match=r"fraises\.yaml not found"):
            _ = resolver.config_path

    def test_returns_path_object(self, tmp_path):
        """config_path always returns a Path object."""
        config_file = tmp_path / "test.yaml"
        config_file.write_text("test")
        environ = {"FRAISIER_CONFIG": str(config_file)}
        resolver = ConfigResolver(environ=environ)
        assert isinstance(resolver.config_path, Path)


class TestLogLevel:
    """ConfigResolver.log_level: resolve FRAISIER_LOG_LEVEL with default."""

    def test_env_var_takes_priority(self):
        """FRAISIER_LOG_LEVEL env var overrides default."""
        environ = {"FRAISIER_LOG_LEVEL": "DEBUG"}
        resolver = ConfigResolver(environ=environ)
        assert resolver.log_level == "DEBUG"

    def test_default_is_info(self):
        """When unset, defaults to 'INFO'."""
        resolver = ConfigResolver(environ={})
        assert resolver.log_level == "INFO"

    def test_returns_string(self):
        """log_level returns a string."""
        resolver = ConfigResolver(environ={"FRAISIER_LOG_LEVEL": "ERROR"})
        assert isinstance(resolver.log_level, str)


class TestWebhookHost:
    """ConfigResolver.webhook_host: resolve FRAISIER_WEBHOOK_HOST with default."""

    def test_env_var_takes_priority(self):
        """FRAISIER_WEBHOOK_HOST env var overrides default."""
        environ = {"FRAISIER_WEBHOOK_HOST": "127.0.0.1"}
        resolver = ConfigResolver(environ=environ)
        assert resolver.webhook_host == "127.0.0.1"

    def test_default_is_0_0_0_0(self):
        """When unset, defaults to '0.0.0.0'."""
        resolver = ConfigResolver(environ={})
        assert resolver.webhook_host == "0.0.0.0"


class TestWebhookPort:
    """ConfigResolver.webhook_port: resolve FRAISIER_WEBHOOK_PORT with default."""

    def test_env_var_takes_priority(self):
        """FRAISIER_WEBHOOK_PORT env var overrides default, converted to int."""
        environ = {"FRAISIER_WEBHOOK_PORT": "9000"}
        resolver = ConfigResolver(environ=environ)
        assert resolver.webhook_port == 9000

    def test_default_is_8080(self):
        """When unset, defaults to 8080."""
        resolver = ConfigResolver(environ={})
        assert resolver.webhook_port == 8080

    def test_returns_int(self):
        """webhook_port always returns an int, even when set via env var."""
        resolver = ConfigResolver(environ={"FRAISIER_WEBHOOK_PORT": "5000"})
        assert isinstance(resolver.webhook_port, int)

    def test_invalid_port_raises(self):
        """Invalid port value raises ValueError."""
        resolver = ConfigResolver(environ={"FRAISIER_WEBHOOK_PORT": "not_a_number"})
        with pytest.raises(ValueError):
            _ = resolver.webhook_port


class TestSystemctlWrapper:
    """ConfigResolver.systemctl_wrapper: resolve FRAISIER_SYSTEMCTL_WRAPPER."""

    def test_env_var_returns_path(self):
        """FRAISIER_SYSTEMCTL_WRAPPER returns env value as str."""
        environ = {"FRAISIER_SYSTEMCTL_WRAPPER": "/usr/bin/custom-systemctl"}
        resolver = ConfigResolver(environ=environ)
        assert resolver.systemctl_wrapper == "/usr/bin/custom-systemctl"

    def test_unset_returns_none(self):
        """When unset, returns None."""
        resolver = ConfigResolver(environ={})
        assert resolver.systemctl_wrapper is None


class TestGitProvider:
    """ConfigResolver.git_provider: resolve FRAISIER_GIT_PROVIDER."""

    def test_env_var_takes_priority(self):
        """FRAISIER_GIT_PROVIDER env var overrides default."""
        environ = {"FRAISIER_GIT_PROVIDER": "gitlab"}
        resolver = ConfigResolver(environ=environ)
        assert resolver.git_provider == "gitlab"

    def test_unset_returns_none(self):
        """When unset, returns None."""
        resolver = ConfigResolver(environ={})
        assert resolver.git_provider is None

"""Tests for config.schema module — dataclass extraction."""

from pathlib import Path

import pytest

from fraisier.config.schema import (
    AuditHookConfig,
    BackupHookConfig,
    DEFAULT_SECURITY,
    DeploymentConfig,
    DiscordHookConfig,
    EmailHookConfig,
    GenericNotificationHookConfig,
    GithubActionsScaffoldConfig,
    HealthConfig,
    HealthResponseConfig,
    MigrationHooksConfig,
    NginxEnvConfig,
    NginxScaffoldConfig,
    NotificationHooksConfig,
    PostgresLoggingConfig,
    ScaffoldConfig,
    SECURITY_DIRECTIVE_MAP,
    ServiceConfig,
    ShipCheckConfig,
    ShipConfig,
    SlackHookConfig,
    SystemdScaffoldConfig,
    TeamsHookConfig,
)


class TestSchemaImports:
    """Test that all schema classes can be imported from schema module."""

    def test_service_config_importable(self):
        """ServiceConfig is importable from fraisier.config.schema."""
        assert ServiceConfig is not None

    def test_nginx_env_config_importable(self):
        """NginxEnvConfig is importable from fraisier.config.schema."""
        assert NginxEnvConfig is not None

    def test_scaffold_config_importable(self):
        """ScaffoldConfig is importable from fraisier.config.schema."""
        assert ScaffoldConfig is not None

    def test_default_security_importable(self):
        """DEFAULT_SECURITY constant is importable from fraisier.config.schema."""
        assert DEFAULT_SECURITY is not None
        assert isinstance(DEFAULT_SECURITY, dict)


class TestBackwardsCompatibility:
    """Test that imports still work from fraisier.config (re-export shim)."""

    def test_service_config_from_config(self):
        """ServiceConfig still importable from fraisier.config."""
        from fraisier.config import ServiceConfig as ConfigServiceConfig

        assert ConfigServiceConfig is ServiceConfig

    def test_default_security_from_config(self):
        """DEFAULT_SECURITY still importable from fraisier.config."""
        from fraisier.config import DEFAULT_SECURITY as ConfigDefaultSecurity

        assert ConfigDefaultSecurity is DEFAULT_SECURITY

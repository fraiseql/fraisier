"""Tests for ServiceConfig, NginxEnvConfig, and RestrictedPath dataclasses."""

import pytest

from fraisier.config import (
    DEFAULT_SECURITY,
    SECURITY_DIRECTIVE_MAP,
    NginxEnvConfig,
    RestrictedPath,
    ServiceConfig,
)
from fraisier.errors import ValidationError


class TestServiceConfigDefaults:
    """ServiceConfig() with no args produces correct defaults."""

    def test_defaults(self):
        sc = ServiceConfig()
        assert sc.user is None
        assert sc.group is None
        assert sc.port is None
        assert sc.workers == 1
        assert sc.exec is None
        assert sc.memory_max is None
        assert sc.memory_high is None
        assert sc.cpu_quota is None
        assert sc.environment_file is None
        assert sc.credentials == {}
        assert sc.environment == {}
        assert sc.security == {}


class TestServiceConfigFromEnvDict:
    """ServiceConfig.from_env_dict() parses nested service: key."""

    def test_nested_service_key(self):
        env = {
            "service": {
                "user": "myapp",
                "group": "www-data",
                "port": 8042,
                "workers": 4,
                "exec": ".venv/bin/uvicorn src.app:app",
                "memory_max": "4G",
                "memory_high": "3G",
                "cpu_quota": "200%",
                "environment_file": "/etc/myapp/api.env",
                "credentials": {"pg_password": "/etc/creds/pg"},
                "environment": {"DB_NAME": "myapp_db"},
                "security": {"protect_home": "read-only"},
            }
        }
        sc = ServiceConfig.from_env_dict(env)
        assert sc.user == "myapp"
        assert sc.group == "www-data"
        assert sc.port == 8042
        assert sc.workers == 4
        assert sc.exec == ".venv/bin/uvicorn src.app:app"
        assert sc.memory_max == "4G"
        assert sc.memory_high == "3G"
        assert sc.cpu_quota == "200%"
        assert sc.environment_file == "/etc/myapp/api.env"
        assert sc.credentials == {"pg_password": "/etc/creds/pg"}
        assert sc.environment == {"DB_NAME": "myapp_db"}
        assert sc.security == {"protect_home": "read-only"}

    def test_empty_env_dict(self):
        sc = ServiceConfig.from_env_dict({})
        assert sc.workers == 1
        assert sc.port is None
        assert sc.user is None


class TestServiceConfigBackwardCompat:
    """Legacy flat fields map correctly to ServiceConfig."""

    def test_flat_fields(self):
        env = {
            "worker_count": 4,
            "memory_max": "8G",
            "exec_command": "/usr/bin/uvicorn src.app:app",
        }
        sc = ServiceConfig.from_env_dict(env)
        assert sc.workers == 4
        assert sc.memory_max == "8G"
        assert sc.exec == "/usr/bin/uvicorn src.app:app"

    def test_nested_takes_precedence_over_flat(self):
        env = {
            "worker_count": 2,
            "memory_max": "2G",
            "exec_command": "old_exec",
            "service": {
                "workers": 8,
                "memory_max": "16G",
                "exec": "new_exec",
            },
        }
        sc = ServiceConfig.from_env_dict(env)
        assert sc.workers == 8
        assert sc.memory_max == "16G"
        assert sc.exec == "new_exec"

    def test_flat_memory_max_used_when_no_service_key(self):
        env = {"memory_max": "4G"}
        sc = ServiceConfig.from_env_dict(env)
        assert sc.memory_max == "4G"


class TestServiceConfigValidation:
    """ServiceConfig validates port and credential paths."""

    def test_invalid_port_too_high(self):
        with pytest.raises(ValidationError, match="port must be 1-65535"):
            ServiceConfig(port=99999)

    def test_invalid_port_zero(self):
        with pytest.raises(ValidationError, match="port must be 1-65535"):
            ServiceConfig(port=0)

    def test_valid_port(self):
        sc = ServiceConfig(port=8080)
        assert sc.port == 8080

    def test_invalid_memory_max(self):
        with pytest.raises(ValidationError, match="memory_max"):
            ServiceConfig(memory_max="4gb")

    def test_invalid_memory_high(self):
        with pytest.raises(ValidationError, match="memory_high"):
            ServiceConfig(memory_high="bad")

    def test_relative_credential_path(self):
        with pytest.raises(ValidationError, match="absolute path"):
            ServiceConfig(credentials={"pg": "relative/path"})

    def test_valid_credential_path(self):
        sc = ServiceConfig(credentials={"pg": "/etc/creds/pg"})
        assert sc.credentials == {"pg": "/etc/creds/pg"}


class TestServiceConfigResolvedSecurity:
    """resolved_security merges user overrides with defaults."""

    def test_default_security_when_no_overrides(self):
        sc = ServiceConfig()
        assert sc.resolved_security == DEFAULT_SECURITY

    def test_override_single_directive(self):
        sc = ServiceConfig(security={"protect_home": "read-only"})
        resolved = sc.resolved_security
        assert resolved["protect_home"] == "read-only"
        # Other defaults still present
        assert resolved["no_new_privileges"] is True
        assert resolved["protect_system"] == "strict"

    def test_add_new_directive(self):
        sc = ServiceConfig(security={"protect_clock": True})
        resolved = sc.resolved_security
        assert resolved["protect_clock"] is True
        # Defaults still present
        assert resolved["private_tmp"] is True


class TestSecurityDirectiveMap:
    """SECURITY_DIRECTIVE_MAP covers all DEFAULT_SECURITY keys."""

    def test_all_defaults_have_mapping(self):
        for key in DEFAULT_SECURITY:
            assert key in SECURITY_DIRECTIVE_MAP, f"Missing mapping for {key}"


class TestRestrictedPathDefaults:
    """RestrictedPath defaults."""

    def test_defaults(self):
        rp = RestrictedPath(path="/admin/")
        assert rp.path == "/admin/"
        assert rp.allow == ["127.0.0.1"]
        assert rp.deny == "all"

    def test_custom_allow_deny(self):
        rp = RestrictedPath(path="/api/", allow=["10.0.0.0/8", "127.0.0.1"], deny="all")
        assert rp.allow == ["10.0.0.0/8", "127.0.0.1"]


class TestNginxEnvConfigDefaults:
    """NginxEnvConfig() with no args produces correct defaults."""

    def test_defaults(self):
        nc = NginxEnvConfig()
        assert nc.server_name is None
        assert nc.ssl_cert is None
        assert nc.ssl_key is None
        assert nc.cors_origins == []
        assert nc.restricted_paths == []


class TestNginxEnvConfigFromEnvDict:
    """NginxEnvConfig.from_env_dict() parses nginx: key."""

    def test_returns_none_without_nginx_key(self):
        assert NginxEnvConfig.from_env_dict({}) is None
        assert NginxEnvConfig.from_env_dict({"other": "stuff"}) is None

    def test_full_parse(self):
        env = {
            "nginx": {
                "server_name": "api.dev",
                "ssl_cert": "/etc/ssl/cert.pem",
                "ssl_key": "/etc/ssl/key.pem",
                "cors_origins": ["https://app.dev"],
                "restricted_paths": [
                    {"path": "/admin/", "allow": ["10.0.0.0/8"], "deny": "all"}
                ],
            }
        }
        nc = NginxEnvConfig.from_env_dict(env)
        assert nc is not None
        assert nc.server_name == "api.dev"
        assert nc.ssl_cert == "/etc/ssl/cert.pem"
        assert nc.ssl_key == "/etc/ssl/key.pem"
        assert nc.cors_origins == ["https://app.dev"]
        assert len(nc.restricted_paths) == 1
        assert nc.restricted_paths[0].path == "/admin/"
        assert nc.restricted_paths[0].allow == ["10.0.0.0/8"]

    def test_string_restricted_paths_backward_compat(self):
        env = {"nginx": {"restricted_paths": ["/admin/", "/utilities/"]}}
        nc = NginxEnvConfig.from_env_dict(env)
        assert nc is not None
        assert len(nc.restricted_paths) == 2
        assert nc.restricted_paths[0].path == "/admin/"
        assert nc.restricted_paths[0].allow == ["127.0.0.1"]
        assert nc.restricted_paths[0].deny == "all"
        assert nc.restricted_paths[1].path == "/utilities/"

    def test_mixed_restricted_paths(self):
        env = {
            "nginx": {
                "restricted_paths": [
                    "/simple/",
                    {"path": "/complex/", "allow": ["10.0.0.0/8"]},
                ]
            }
        }
        nc = NginxEnvConfig.from_env_dict(env)
        assert nc is not None
        assert nc.restricted_paths[0].path == "/simple/"
        assert nc.restricted_paths[0].allow == ["127.0.0.1"]
        assert nc.restricted_paths[1].path == "/complex/"
        assert nc.restricted_paths[1].allow == ["10.0.0.0/8"]


class TestNginxEnvConfigValidation:
    """NginxEnvConfig validates SSL cert/key pairing."""

    def test_ssl_cert_without_key_raises(self):
        with pytest.raises(ValidationError, match="ssl_key"):
            NginxEnvConfig(ssl_cert="/path/cert.pem")

    def test_ssl_key_without_cert_raises(self):
        with pytest.raises(ValidationError, match="ssl_cert"):
            NginxEnvConfig(ssl_key="/path/key.pem")

    def test_both_ssl_cert_and_key_valid(self):
        nc = NginxEnvConfig(ssl_cert="/path/cert.pem", ssl_key="/path/key.pem")
        assert nc.ssl_cert == "/path/cert.pem"

    def test_neither_ssl_cert_nor_key_valid(self):
        nc = NginxEnvConfig()
        assert nc.ssl_cert is None

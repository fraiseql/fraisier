"""Security hardening tests — Phase 2.

Tests for input validation, signature verification, rate limiting,
path traversal, template injection, and CORS origin validation.
"""

import pytest

from fraisier.config import NginxEnvConfig, ServiceConfig
from fraisier.dbops.backup import run_backup
from fraisier.dbops.restore import restore_backup
from fraisier.errors import ValidationError
from fraisier.git.bitbucket import Bitbucket
from fraisier.status import DeploymentStatusFile, read_status, write_status
from fraisier.webhook_rate_limit import check_rate_limit, reset

# ---------------------------------------------------------------------------
# Cycle 1: dbops input validation
# ---------------------------------------------------------------------------


class TestBackupInputValidation:
    """Backup inputs must be validated before shell execution."""

    def test_excluded_table_with_flag_injection(self):
        """excluded_tables containing '--file=/etc/passwd' must be rejected."""
        with pytest.raises(ValueError, match="Invalid"):
            run_backup(
                db_name="proddb",
                output_dir="/backups",
                mode="slim",
                excluded_tables=["--file=/etc/passwd"],
            )

    def test_backup_path_traversal_rejected(self):
        """backup_path with path traversal must be rejected."""
        with pytest.raises(ValueError, match=r"Invalid|traversal"):
            run_backup(
                db_name="proddb",
                output_dir="../../etc/shadow",
            )

    def test_compression_with_shell_injection(self):
        """compression containing shell metacharacters must be rejected."""
        with pytest.raises(ValueError, match=r"Invalid|compression"):
            run_backup(
                db_name="proddb",
                output_dir="/backups",
                compression="; rm -rf /",
            )

    def test_compression_valid_formats(self):
        """Valid compression specs must be accepted."""
        from unittest.mock import MagicMock, patch

        for comp in ("zstd:9", "lz4", "gzip:5", "none"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = run_backup(
                    db_name="proddb", output_dir="/backups", compression=comp
                )
            assert result.success is True


class TestRestoreInputValidation:
    """Restore inputs must be validated before shell execution."""

    def test_backup_path_traversal_rejected(self):
        """backup_path with '../../' must be rejected."""
        with pytest.raises(ValueError, match=r"Invalid|traversal"):
            restore_backup(
                backup_path="../../etc/shadow",
                db_name="staging",
            )

    def test_restore_uses_psql_variable_binding(self):
        """db_owner must not be interpolated via f-string in SQL."""
        from unittest.mock import patch

        with patch("fraisier.dbops.restore._pg_cmd") as mock_cmd:
            mock_cmd.return_value = (0, "", "")
            restore_backup(
                backup_path="/backups/prod.dump",
                db_name="staging",
                db_owner="appuser",
            )

        # The REASSIGN OWNED call should use psql -v variable binding,
        # not an f-string with the owner name directly in the SQL
        reassign_call = mock_cmd.call_args_list[1]
        cmd_args = reassign_call[0][0]
        # Should contain -v for variable binding
        assert "-v" in cmd_args, (
            "REASSIGN OWNED should use psql -v variable binding, not f-string SQL"
        )


# ---------------------------------------------------------------------------
# Cycle 2: Bitbucket Cloud signature bypass
# ---------------------------------------------------------------------------


class TestBitbucketCloudSignatureBypass:
    """Bitbucket Cloud must reject unsigned requests when secret is configured."""

    def test_cloud_unsigned_request_rejected(self):
        """Cloud webhook with no signature header and configured secret -> False."""
        provider = Bitbucket({"webhook_secret": "my-secret", "server": False})
        result = provider.verify_webhook_signature(b'{"test": "data"}', {})
        assert result is False

    def test_cloud_unsigned_request_rejected_default(self):
        """Cloud is the default mode — unsigned request must still be rejected."""
        provider = Bitbucket({"webhook_secret": "my-secret"})
        result = provider.verify_webhook_signature(b'{"test": "data"}', {})
        assert result is False

    def test_server_unsigned_request_rejected(self):
        """Server mode with no signature header -> False."""
        provider = Bitbucket({"webhook_secret": "my-secret", "server": True})
        result = provider.verify_webhook_signature(b'{"test": "data"}', {})
        assert result is False


# ---------------------------------------------------------------------------
# Cycle 3: Rate limiter production readiness
# ---------------------------------------------------------------------------


class TestRateLimiterProxy:
    """Rate limiter must use real client IP behind reverse proxy."""

    def setup_method(self):
        reset()

    def test_requests_from_same_proxy_ip_differentiated(self):
        """Requests forwarded by 127.0.0.1 with different X-Forwarded-For
        should not share one bucket."""
        from fraisier.webhook_rate_limit import get_client_ip

        # Two different real IPs behind the same proxy
        ip1 = get_client_ip(
            "127.0.0.1",
            headers={"x-forwarded-for": "203.0.113.1"},
            trusted_proxies={"127.0.0.1"},
        )
        ip2 = get_client_ip(
            "127.0.0.1",
            headers={"x-forwarded-for": "203.0.113.2"},
            trusted_proxies={"127.0.0.1"},
        )
        assert ip1 != ip2

    def test_untrusted_proxy_ignores_forwarded_header(self):
        """X-Forwarded-For from untrusted proxy should be ignored."""
        from fraisier.webhook_rate_limit import get_client_ip

        ip = get_client_ip(
            "10.0.0.5",
            headers={"x-forwarded-for": "203.0.113.1"},
            trusted_proxies={"127.0.0.1"},
        )
        assert ip == "10.0.0.5"

    def test_lru_eviction_beyond_max_ips(self):
        """Rate limiter must evict oldest entries when tracking >256 IPs."""
        for i in range(300):
            check_rate_limit(f"10.0.{i // 256}.{i % 256}")

        # After 300 unique IPs, the first ones should have been evicted.
        # A new request from the first IP should be allowed (fresh bucket).
        assert check_rate_limit("10.0.0.0") is True


# ---------------------------------------------------------------------------
# Cycle 4: Path traversal in status.py and webhook
# ---------------------------------------------------------------------------


class TestStatusPathTraversal:
    """Path traversal must be blocked in status file operations."""

    def test_write_status_traversal_rejected(self, tmp_path):
        """write_status with traversal fraise_name must be rejected."""
        status = DeploymentStatusFile(
            fraise_name="../../../etc/cron.d/evil",
            environment="production",
        )
        with pytest.raises((ValueError, ValidationError), match=r"Invalid|traversal"):
            write_status(status, status_dir=tmp_path)

    def test_read_status_traversal_rejected(self, tmp_path):
        """read_status with traversal fraise_name must be rejected."""
        with pytest.raises((ValueError, ValidationError), match=r"Invalid|traversal"):
            read_status("../../../etc/passwd", status_dir=tmp_path)

    def test_webhook_status_endpoint_traversal(self):
        """GET /api/status with path-traversal name must return 400."""
        from fastapi.testclient import TestClient

        from fraisier.webhook import app

        client = TestClient(app)
        # Use a name with dots and slashes that would cause traversal
        response = client.get("/api/status/evil..name")
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Cycle 5: Systemd template injection
# ---------------------------------------------------------------------------


class TestSystemdTemplateInjection:
    """ServiceConfig must reject values that could inject into systemd units."""

    def test_env_var_value_with_newline_rejected(self):
        """Environment variable value containing newline must be rejected."""
        with pytest.raises(ValidationError, match=r"newline|Newline"):
            ServiceConfig(environment={"DB_NAME": "mydb\nExecStart=/bin/sh"})

    def test_env_var_name_with_newline_rejected(self):
        """Environment variable name containing newline must be rejected."""
        with pytest.raises(ValidationError, match=r"newline|Newline"):
            ServiceConfig(environment={"DB\nNAME": "mydb"})

    def test_exec_command_with_semicolon_rejected(self):
        """exec with shell metacharacter ';' must be rejected."""
        with pytest.raises(ValidationError, match=r"metacharacter|invalid"):
            ServiceConfig(exec="/bin/app; rm -rf /")

    def test_exec_command_with_ampersand_rejected(self):
        """exec with shell metacharacter '&&' must be rejected."""
        with pytest.raises(ValidationError, match=r"metacharacter|invalid"):
            ServiceConfig(exec="/bin/app && malicious")

    def test_valid_exec_command_accepted(self):
        """Normal exec commands must be accepted."""
        sc = ServiceConfig(exec="/usr/bin/uvicorn app:main --host 0.0.0.0 --port 8080")
        assert sc.exec == "/usr/bin/uvicorn app:main --host 0.0.0.0 --port 8080"

    def test_valid_environment_accepted(self):
        """Normal environment variables must be accepted."""
        sc = ServiceConfig(environment={"DB_NAME": "myapp_db", "PORT": "8080"})
        assert sc.environment == {"DB_NAME": "myapp_db", "PORT": "8080"}


# ---------------------------------------------------------------------------
# Cycle 6: CORS origin validation
# ---------------------------------------------------------------------------


class TestCorsOriginValidation:
    """CORS origins must have dots properly escaped for nginx regex matching."""

    def test_literal_domain_dots_escaped(self):
        """'example.com' must be auto-escaped to 'example\\.com' via property."""
        nc = NginxEnvConfig(cors_origins=["https://example.com"])
        escaped = nc.cors_origins_escaped
        assert escaped == [r"https://example\.com"]

    def test_already_escaped_dots_not_double_escaped(self):
        """Already-escaped 'example\\.com' must not become 'example\\\\.com'."""
        nc = NginxEnvConfig(cors_origins=[r"https://example\.com"])
        escaped = nc.cors_origins_escaped
        assert r"\\." not in escaped[0]

    def test_regex_pattern_preserved(self):
        """Explicit regex patterns like '.*\\.example\\.com' must be preserved."""
        nc = NginxEnvConfig(cors_origins=[r"https://.*\.example\.com"])
        escaped = nc.cors_origins_escaped
        assert escaped[0] == r"https://.*\.example\.com"

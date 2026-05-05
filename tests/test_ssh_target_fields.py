"""Tests for SshTarget.db_path and fraisier_bin fields (issue #194)."""

from __future__ import annotations

from fraisier.ssh import SshTarget


class TestSshTargetNewFields:
    def test_accepts_db_path_and_fraisier_bin(self):
        cfg = {
            "host": "prod.example.com",
            "user": "deploy",
            "db_path": "/var/lib/fraisier/fraisier.db",
            "fraisier_bin": "/home/deploy/.local/bin/fraisier",
        }
        target = SshTarget.from_config(cfg)
        assert target.db_path == "/var/lib/fraisier/fraisier.db"
        assert target.fraisier_bin == "/home/deploy/.local/bin/fraisier"

    def test_db_path_defaults_to_none(self):
        cfg = {"host": "prod.example.com"}
        target = SshTarget.from_config(cfg)
        assert target.db_path is None

    def test_fraisier_bin_defaults_to_fraisier(self):
        cfg = {"host": "prod.example.com"}
        target = SshTarget.from_config(cfg)
        assert target.fraisier_bin == "fraisier"

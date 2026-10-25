"""`fraisier scaffold-install` runs install.sh with the operator's flags only."""

from __future__ import annotations

from fraisier.cli import scaffold as scaffold_cli


def test_a_deploy_variable_in_the_environment_adds_no_flag(monkeypatch):
    """No deploy runs scaffold-install since #486, so the variable can only be
    stale, and install.sh no longer accepts the flag it used to become (#489)."""
    monkeypatch.setenv("FRAISIER_DEPLOY_IN_FLIGHT", "1")
    cmd = scaffold_cli._build_install_cmd(
        "/tmp/install.sh", dry_run=True, validate_only=False, verbose=True
    )
    assert cmd == ["sudo", "/tmp/install.sh", "--dry-run", "--verbose"]

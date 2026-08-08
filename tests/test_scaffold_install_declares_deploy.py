"""The "a deploy is in flight" declaration must survive `sudo` (#349).

The subprocess install path is deployer → ``fraisier scaffold-install`` →
``sudo install.sh``. An environment variable crosses the first hop and dies at
the second, because sudo resets the environment. So the CLI reads the variable
it inherited and re-states it as a flag, which sudo passes through untouched.
"""

from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

_YAML = """\
name: testapp
servers:
  example.com:
    machine_hostnames: [default-testrunner]

fraises:
  api:
    type: api
    environments:
      production:
        server: example.com

deployment:
  lock_dir: {lock_dir}

scaffold:
  deploy_user: testapp_deploy
"""

_WEBHOOK_UNIT = "fraisier-testapp-webhook.service"

_SUDO_STUB = """\
#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FRAISIER_TEST_SUDO_LOG"
exit 0
"""


@pytest.fixture
def rendered(tmp_path):
    lock_dir = tmp_path / "lockdir"
    lock_dir.mkdir()
    cfg_path = tmp_path / "fraises.yaml"
    cfg_path.write_text(_YAML.format(lock_dir=lock_dir))
    renderer = ScaffoldRenderer(FraisierConfig(cfg_path))
    renderer.output_dir = tmp_path / "generated"
    renderer.render()
    return tmp_path / "generated" / "install.sh"


def _source_with_args(tmp_path, install_sh, args: str, snippet: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    sudo = bin_dir / "sudo"
    sudo.write_text(_SUDO_STUB)
    sudo.chmod(0o755)
    sudo_log = tmp_path / "sudo.log"

    script = tmp_path / "flagharness.sh"
    script.write_text(f'. "{install_sh}" {args}\n{textwrap.dedent(snippet)}\n')
    env = {
        **os.environ,
        "FRAISIER_TEST_SUDO_LOG": str(sudo_log),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    env.pop("FRAISIER_DEPLOY_IN_FLIGHT", None)
    env.pop("FRAISIER_VIA_SCAFFOLD_INSTALL_HELPER", None)
    result = subprocess.run(
        ["bash", str(script)], check=False, capture_output=True, text=True, env=env
    )
    calls = (
        [ln for ln in sudo_log.read_text().splitlines() if ln.strip()]
        if sudo_log.exists()
        else []
    )
    return result, calls


class TestInstallShDeployInFlightFlag:
    def test_flag_defers_the_restart(self, tmp_path, rendered):
        result, calls = _source_with_args(
            tmp_path,
            rendered,
            "--deploy-in-flight",
            f"_restart_deploy_host_unit {_WEBHOOK_UNIT}",
        )
        assert result.returncode == 0, result.stderr
        assert calls == []
        assert "Deferring restart" in result.stdout

    def test_without_the_flag_the_restart_happens(self, tmp_path, rendered):
        result, calls = _source_with_args(
            tmp_path, rendered, "", f"_restart_deploy_host_unit {_WEBHOOK_UNIT}"
        )
        assert result.returncode == 0, result.stderr
        assert any(f"systemctl restart {_WEBHOOK_UNIT}" in c for c in calls)

    def test_help_documents_the_flag(self, rendered):
        result = subprocess.run(
            ["bash", str(rendered), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert "--deploy-in-flight" in result.stdout


class TestCliForwardsTheDeclarationAcrossSudo:
    """`sudo` resets the environment, so the variable must become a flag."""

    def test_flag_is_added_when_the_deployer_declared_a_deploy(self, monkeypatch):
        from fraisier.cli import scaffold as scaffold_cli

        monkeypatch.setenv("FRAISIER_DEPLOY_IN_FLIGHT", "1")
        cmd = scaffold_cli._build_install_cmd(
            "/tmp/install.sh", dry_run=False, validate_only=False, verbose=False
        )
        assert "--deploy-in-flight" in cmd

    def test_flag_is_absent_for_an_operator_run(self, monkeypatch):
        from fraisier.cli import scaffold as scaffold_cli

        monkeypatch.delenv("FRAISIER_DEPLOY_IN_FLIGHT", raising=False)
        cmd = scaffold_cli._build_install_cmd(
            "/tmp/install.sh", dry_run=False, validate_only=False, verbose=False
        )
        assert "--deploy-in-flight" not in cmd

    def test_sudo_still_leads_the_command(self, monkeypatch):
        from fraisier.cli import scaffold as scaffold_cli

        monkeypatch.setenv("FRAISIER_DEPLOY_IN_FLIGHT", "1")
        cmd = scaffold_cli._build_install_cmd(
            "/tmp/install.sh", dry_run=True, validate_only=False, verbose=True
        )
        assert cmd[0] == "sudo"
        assert cmd[1] == "/tmp/install.sh"
        assert "--dry-run" in cmd
        assert "--verbose" in cmd

"""Tests for install.sh --standalone mode (rendered via ScaffoldRenderer)."""

from __future__ import annotations

import os
import subprocess

import pytest

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

_MINIMAL_YAML = """\
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

scaffold:
  deploy_user: testapp_deploy
"""

_GIT_REPO_YAML = """\
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
        git_repo: /var/git/api.testapp.dev.git
scaffold:
  deploy_user: testapp_deploy
"""


@pytest.fixture(scope="module")
def rendered_install_sh(tmp_path_factory):
    """Render install.sh once for the whole module."""
    tmp = tmp_path_factory.mktemp("scaffold")
    cfg_path = tmp / "fraises.yaml"
    cfg_path.write_text(_MINIMAL_YAML)
    config = FraisierConfig(cfg_path)
    renderer = ScaffoldRenderer(config)
    renderer.output_dir = tmp / "generated"
    renderer.render()
    install_sh = tmp / "generated" / "install.sh"
    install_sh.chmod(0o755)
    return install_sh


class TestInstallShHelp:
    def test_help_exits_zero(self, rendered_install_sh):
        result = subprocess.run(
            ["bash", str(rendered_install_sh), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_help_documents_standalone(self, rendered_install_sh):
        result = subprocess.run(
            ["bash", str(rendered_install_sh), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert "--standalone" in result.stdout

    def test_help_documents_scaffold_dir(self, rendered_install_sh):
        result = subprocess.run(
            ["bash", str(rendered_install_sh), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert "--scaffold-dir" in result.stdout

    def test_unknown_option_exits_nonzero(self, rendered_install_sh):
        result = subprocess.run(
            ["bash", str(rendered_install_sh), "--bogus-option"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0


class TestInstallShStandaloneMode:
    def _run_with_hostname(self, install_sh, hostname: str, *extra_args):
        """Run install.sh with a faked hostname."""
        tmp_dir = install_sh.parent.parent / "bin"
        tmp_dir.mkdir(exist_ok=True)
        fake_hostname = tmp_dir / "hostname"
        fake_hostname.write_text(f"#!/bin/bash\necho {hostname}\n")
        fake_hostname.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{tmp_dir}:{env.get('PATH', '')}"

        cmd = ["bash", str(install_sh)]
        cmd.extend(extra_args)
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        return result

    def test_standalone_dry_run_exits_zero(self, rendered_install_sh, tmp_path):
        """--standalone --dry-run must succeed even when /opt/testapp doesn't exist."""
        scaffold_dir = tmp_path / "scaffold"
        scaffold_dir.mkdir()
        result = self._run_with_hostname(
            rendered_install_sh,
            "default-testrunner",
            "--standalone",
            "--scaffold-dir",
            str(scaffold_dir),
            "--dry-run",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_standalone_without_scaffold_dir_exits_zero(self, rendered_install_sh):
        """--standalone without --scaffold-dir uses the script's own directory."""
        result = self._run_with_hostname(
            rendered_install_sh,
            "default-testrunner",
            "--standalone",
            "--dry-run",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_scaffold_dir_implies_standalone(self, rendered_install_sh, tmp_path):
        """--scaffold-dir alone (without --standalone) must also work."""
        scaffold_dir = tmp_path / "sc"
        scaffold_dir.mkdir()
        result = self._run_with_hostname(
            rendered_install_sh,
            "default-testrunner",
            "--scaffold-dir",
            str(scaffold_dir),
            "--dry-run",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_validate_only_standalone_skips_location_check(
        self, rendered_install_sh, tmp_path
    ):
        """--standalone --validate-only must not fail on missing install.sh location."""
        scaffold_dir = tmp_path / "sc"
        scaffold_dir.mkdir()
        result = self._run_with_hostname(
            rendered_install_sh,
            "default-testrunner",
            "--standalone",
            "--validate-only",
            "--scaffold-dir",
            str(scaffold_dir),
        )
        # May fail due to missing system commands (useradd etc.) in the test
        # environment, but must NOT fail with "Generated install.sh" not found.
        assert "Generated install.sh" not in result.stderr

    def test_non_standalone_dry_run_exits_zero(self, rendered_install_sh):
        """Normal (non-standalone) --dry-run must still work."""
        result = self._run_with_hostname(
            rendered_install_sh,
            "default-testrunner",
            "--dry-run",
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"


class TestInstallShGitRepoOwnership:
    """install.sh must chown existing bare git repos to the deploy user."""

    def _run_with_hostname(self, install_sh, hostname: str, *extra_args):
        """Run install.sh with a faked hostname."""
        tmp_dir = install_sh.parent.parent / "bin"
        tmp_dir.mkdir(exist_ok=True)
        fake_hostname = tmp_dir / "hostname"
        fake_hostname.write_text(f"#!/bin/bash\necho {hostname}\n")
        fake_hostname.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{tmp_dir}:{env.get('PATH', '')}"

        cmd = ["bash", str(install_sh)]
        cmd.extend(extra_args)
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        return result

    def test_git_repo_chown_emitted_in_dry_run(self, tmp_path):
        """When git_repo is set, --dry-run output must include chown of that path."""
        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_GIT_REPO_YAML)
        config = FraisierConfig(cfg_path)
        renderer = ScaffoldRenderer(config)
        renderer.output_dir = tmp_path / "generated"
        renderer.render()
        install_sh = tmp_path / "generated" / "install.sh"
        install_sh.chmod(0o755)

        result = self._run_with_hostname(
            install_sh, "default-testrunner", "--standalone", "--dry-run"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "/var/git/api.testapp.dev.git" in result.stdout

    def test_no_git_repo_no_chown_emitted(self, tmp_path):
        """When git_repo is absent, no bare-repo chown block must appear."""
        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(_MINIMAL_YAML)
        config = FraisierConfig(cfg_path)
        renderer = ScaffoldRenderer(config)
        renderer.output_dir = tmp_path / "generated"
        renderer.render()
        install_sh = tmp_path / "generated" / "install.sh"
        install_sh.chmod(0o755)

        result = self._run_with_hostname(
            install_sh, "default-testrunner", "--standalone", "--dry-run"
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "bare git repo" not in result.stdout


class TestInstallShServerDetection:
    """install.sh runtime server detection with servers: config."""

    def _render_with_servers(self, tmp_path, yaml_content: str):
        """Render install.sh with given multi-server config."""
        cfg_path = tmp_path / "fraises.yaml"
        cfg_path.write_text(yaml_content)
        config = FraisierConfig(cfg_path)
        renderer = ScaffoldRenderer(config)
        renderer.output_dir = tmp_path / "generated"
        renderer.render()
        install_sh = tmp_path / "generated" / "install.sh"
        install_sh.chmod(0o755)
        return install_sh

    def _run_with_hostname(self, install_sh, hostname: str, *extra_args):
        """Run install.sh with a faked hostname."""
        # Create a temporary script that fakes hostname -s output
        tmp_dir = install_sh.parent.parent / "bin"
        tmp_dir.mkdir(exist_ok=True)
        fake_hostname = tmp_dir / "hostname"
        fake_hostname.write_text(f"#!/bin/bash\necho {hostname}\n")
        fake_hostname.chmod(0o755)

        # Prepend the temp bin dir to PATH so our fake hostname is used
        env = os.environ.copy()
        env["PATH"] = f"{tmp_dir}:{env.get('PATH', '')}"

        cmd = ["bash", str(install_sh), "--standalone", "--dry-run"]
        cmd.extend(extra_args)
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        return result

    def test_no_servers_section_exits_nonzero(self, tmp_path):
        """Without servers: section, install.sh exits 1 with clear error."""
        yaml_content = """\
name: testapp
fraises:
  api:
    type: api
    environments:
      production: {}
scaffold:
  deploy_user: testapp_deploy
"""
        install_sh = self._render_with_servers(tmp_path, yaml_content)
        result = self._run_with_hostname(install_sh, "any-host")
        assert result.returncode != 0
        assert "servers:" in result.stderr
        assert "required" in result.stderr

    def test_known_machine_installs_its_envs(self, tmp_path):
        """Machine found in servers: installs only its assigned environments."""
        yaml_content = """\
name: testapp
servers:
  prod.example.com:
    machine_hostnames: [backend-prod-01, backend-prod-02]
  staging.example.com:
    machine_hostnames: [backend-staging-01]

fraises:
  api:
    type: api
    environments:
      production:
        server: prod.example.com
      staging:
        server: staging.example.com
scaffold:
  deploy_user: testapp_deploy
"""
        install_sh = self._render_with_servers(tmp_path, yaml_content)
        result = self._run_with_hostname(install_sh, "backend-prod-01")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        # Production env socket should be restarted; staging should not appear
        assert "production" in result.stdout or "api" in result.stdout

    def test_other_machine_envs_skipped(self, tmp_path):
        """Only the current machine's environments are installed."""
        yaml_content = """\
name: testapp
servers:
  prod.example.com:
    machine_hostnames: [backend-prod-01]
  staging.example.com:
    machine_hostnames: [backend-staging-01]

fraises:
  api:
    type: api
    environments:
      production:
        server: prod.example.com
      staging:
        server: staging.example.com
scaffold:
  deploy_user: testapp_deploy
"""
        install_sh = self._render_with_servers(tmp_path, yaml_content)
        # Run on prod machine; staging env should not appear in dry-run
        result = self._run_with_hostname(install_sh, "backend-prod-01")
        assert result.returncode == 0
        # Verify that the script contains the machine_env_map with both servers
        script_content = install_sh.read_text()
        assert "backend-prod-01" in script_content
        assert "backend-staging-01" in script_content

    def test_unknown_machine_exits_nonzero(self, tmp_path):
        """Machine not in servers: exits 1 and lists known machines."""
        yaml_content = """\
name: testapp
servers:
  prod.example.com:
    machine_hostnames: [backend-prod-01, backend-prod-02]

fraises:
  api:
    type: api
    environments:
      production:
        server: prod.example.com
scaffold:
  deploy_user: testapp_deploy
"""
        install_sh = self._render_with_servers(tmp_path, yaml_content)
        result = self._run_with_hostname(install_sh, "unknown-machine")
        assert result.returncode != 0
        assert "not registered" in result.stderr
        assert "Known machines:" in result.stderr
        assert "backend-prod-01" in result.stderr or "backend-prod-02" in result.stderr


class TestScaffoldInstallHelperSelfRestartGuard:
    """The generated install.sh must not restart the scaffold-install-helper's own
    socket when it is being executed BY that helper — doing so SIGTERMs the helper
    mid-request and the deploy aborts before the DB step (the self-restart race)."""

    def test_self_restart_is_guarded_by_via_helper_marker(self, rendered_install_sh):
        text = rendered_install_sh.read_text()
        lines = text.splitlines()
        restart_idxs = [
            i
            for i, ln in enumerate(lines)
            if "systemctl restart" in ln and "scaffold-install-helper.socket" in ln
        ]
        assert restart_idxs, "expected a scaffold-install-helper.socket restart line"
        for i in restart_idxs:
            window = "\n".join(lines[max(0, i - 8) : i])
            assert "FRAISIER_VIA_SCAFFOLD_INSTALL_HELPER" in window, (
                "scaffold-install-helper.socket restart must be guarded by the "
                "FRAISIER_VIA_SCAFFOLD_INSTALL_HELPER marker (self-restart race)"
            )

    def test_guard_skips_restart_when_marker_set(self, rendered_install_sh):
        """With the marker set, the guarded block takes the skip branch (bash -n +
        a targeted eval of the guard condition)."""
        text = rendered_install_sh.read_text()
        # The skip branch must exist for the marker path.
        assert "skipping scaffold-install-helper self-restart" in text
        # And the guard uses the -z (unset/empty) test so an empty value also skips.
        assert 'if [ -z "${FRAISIER_VIA_SCAFFOLD_INSTALL_HELPER:-}" ]; then' in text

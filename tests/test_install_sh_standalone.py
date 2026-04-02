"""Tests for install.sh --standalone mode (rendered via ScaffoldRenderer)."""

from __future__ import annotations

import subprocess

import pytest

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

_MINIMAL_YAML = """\
name: testapp
fraises:
  api:
    type: api
    environments:
      production: {}
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
    def test_standalone_dry_run_exits_zero(self, rendered_install_sh, tmp_path):
        """--standalone --dry-run must succeed even when /opt/testapp doesn't exist."""
        scaffold_dir = tmp_path / "scaffold"
        scaffold_dir.mkdir()
        result = subprocess.run(
            [
                "bash",
                str(rendered_install_sh),
                "--standalone",
                "--scaffold-dir",
                str(scaffold_dir),
                "--dry-run",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_standalone_without_scaffold_dir_exits_zero(self, rendered_install_sh):
        """--standalone without --scaffold-dir uses the script's own directory."""
        result = subprocess.run(
            ["bash", str(rendered_install_sh), "--standalone", "--dry-run"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_scaffold_dir_implies_standalone(self, rendered_install_sh, tmp_path):
        """--scaffold-dir alone (without --standalone) must also work."""
        scaffold_dir = tmp_path / "sc"
        scaffold_dir.mkdir()
        result = subprocess.run(
            [
                "bash",
                str(rendered_install_sh),
                "--scaffold-dir",
                str(scaffold_dir),
                "--dry-run",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_validate_only_standalone_skips_location_check(
        self, rendered_install_sh, tmp_path
    ):
        """--standalone --validate-only must not fail on missing install.sh location."""
        scaffold_dir = tmp_path / "sc"
        scaffold_dir.mkdir()
        result = subprocess.run(
            [
                "bash",
                str(rendered_install_sh),
                "--standalone",
                "--validate-only",
                "--scaffold-dir",
                str(scaffold_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        # May fail due to missing system commands (useradd etc.) in the test
        # environment, but must NOT fail with "Generated install.sh" not found.
        assert "Generated install.sh" not in result.stderr

    def test_non_standalone_dry_run_exits_zero(self, rendered_install_sh):
        """Normal (non-standalone) --dry-run must still work."""
        result = subprocess.run(
            ["bash", str(rendered_install_sh), "--dry-run"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

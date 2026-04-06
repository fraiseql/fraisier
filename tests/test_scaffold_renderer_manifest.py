"""Tests for ScaffoldRenderer integration with PathManifest."""

from pathlib import Path

import pytest

from fraisier.config import FraisierConfig
from fraisier.manifest import PathManifest
from fraisier.scaffold.renderer import _build_context


class TestScaffoldRendererManifest:
    """ScaffoldRenderer passes PathManifest to template contexts."""

    def test_build_context_includes_manifest(self, tmp_path):
        """_build_context returns a dict with manifest key containing PathManifest."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
name: test-project
scaffold:
  output_dir: scripts/generated
  deploy_user: fraisier
  config_path: /opt/fraisier/fraises.yaml

fraises:
  my_api:
    type: api
    description: Test API
    environments:
      development:
        app_path: /var/www/my_api
        git_repo: /var/repos/my_api.git
        systemd_service: my_api-dev.service
"""
        )
        config = FraisierConfig(str(config_file))

        context = _build_context(config)

        # Assert manifest key exists in context
        assert "manifest" in context
        assert isinstance(context["manifest"], PathManifest)

    def test_manifest_contains_global_paths(self, tmp_path):
        """Manifest in context contains all global paths."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
name: test-project
scaffold:
  output_dir: scripts/generated
  deploy_user: fraisier
  config_path: /opt/fraisier/fraises.yaml

fraises:
  my_api:
    type: api
    environments:
      dev:
        app_path: /var/www/api
"""
        )
        config = FraisierConfig(str(config_file))

        context = _build_context(config)
        manifest = context["manifest"]
        all_paths = list(manifest.all_paths())
        path_strs = [str(p.path) for p in all_paths]

        # Global paths should be present
        assert "/opt/fraisier" in path_strs
        assert "/var/lib/fraisier" in path_strs
        assert "/run/fraisier" in path_strs

    def test_manifest_contains_env_paths(self, tmp_path):
        """Manifest in context contains per-environment paths."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
name: test-project
scaffold:
  output_dir: scripts/generated
  deploy_user: fraisier
  config_path: /opt/fraisier/fraises.yaml

fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/my_api/prod
        git_repo: /var/repos/my_api.git
"""
        )
        config = FraisierConfig(str(config_file))

        context = _build_context(config)
        manifest = context["manifest"]
        all_paths = list(manifest.all_paths())
        path_strs = [str(p.path) for p in all_paths]

        # Env paths should be present
        assert "/var/www/my_api/prod" in path_strs
        assert "/var/repos/my_api.git" in path_strs

    def test_manifest_read_write_units(self, tmp_path):
        """Paths in manifest have correct read_write_units for their services."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(
            """
name: myapp
scaffold:
  output_dir: scripts/generated
  deploy_user: fraisier
  config_path: /opt/fraisier/fraises.yaml

fraises:
  api:
    type: api
    environments:
      dev:
        app_path: /var/www/api
        git_repo: /var/repos/api.git
"""
        )
        config = FraisierConfig(str(config_file))

        context = _build_context(config)
        manifest = context["manifest"]

        # Check that paths have the deploy unit listed
        deploy_paths = list(manifest.paths_for_unit("myapp-deploy.service"))
        assert len(deploy_paths) > 0

        # At least git_repo and app_path should be in there
        path_strs = [str(p.path) for p in deploy_paths]
        assert "/var/repos/api.git" in path_strs or "/var/www/api" in path_strs

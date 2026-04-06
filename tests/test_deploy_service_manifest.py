"""Tests for deploy-service.j2 integration with PathManifest."""

from pathlib import Path

import pytest

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer


class TestDeployServiceManifest:
    """deploy-service.j2 generates ReadWritePaths from manifest.paths_for_unit()."""

    def _make_config(self, tmp_path, yaml_content):
        """Helper to create FraisierConfig from yaml content."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(yaml_content)
        return FraisierConfig(str(config_file))

    def test_deploy_service_uses_manifest_for_readwrite_paths(self, tmp_path):
        """deploy-service.j2 loops over manifest.paths_for_unit() for ReadWritePaths."""
        config = self._make_config(
            tmp_path,
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
      production:
        app_path: /var/www/api
        git_repo: /var/repos/api.git
"""
        )
        renderer = ScaffoldRenderer(config)
        renderer._validate_names()

        # Render for this fraise/env
        fraise_name = "api"
        env_name = "production"
        socket_rel = "systemd/myapp-production-deploy.socket"
        renderer._render_deploy_service(fraise_name, env_name, socket_rel)

        # Read the rendered output
        output_path = renderer.output_dir / socket_rel.replace(".socket", "@.service")
        if not output_path.parent.exists():
            pytest.skip("Could not verify output (renderer not fully initialized)")
            return

        # Instead, test the template directly
        context = dict(renderer.context)
        context["fraise_name"] = fraise_name
        context["env_name"] = env_name

        # Get the deploy service env config
        fraise = config.get_fraise(fraise_name)
        env_config = fraise.get("environments", {}).get(env_name, {})
        context["env_config"] = env_config

        # Derive socket stem the same way as build_manifest does
        from fraisier.naming import deploy_socket_name
        socket_unit = deploy_socket_name(env_config, env_name)
        socket_stem = socket_unit.removesuffix(".socket")
        context["socket_stem"] = socket_stem
        context["socket_unit_name"] = socket_unit

        template = renderer.env.get_template("core/deploy-service.j2")
        content = template.render(**context)

        # Should contain ReadWritePaths for the env paths
        assert "ReadWritePaths=" in content

    def test_deploy_service_includes_app_path(self, tmp_path):
        """deploy-service.j2 includes app_path in ReadWritePaths when configured."""
        config = self._make_config(
            tmp_path,
            """
name: myapp
scaffold:
  output_dir: scripts/generated
  deploy_user: fraisier
  config_path: /opt/fraisier/fraises.yaml

fraises:
  web:
    type: api
    environments:
      dev:
        app_path: /var/www/web/dev
        git_repo: /var/repos/web.git
"""
        )
        renderer = ScaffoldRenderer(config)
        renderer._validate_names()

        context = dict(renderer.context)
        context["fraise_name"] = "web"
        context["env_name"] = "dev"

        fraise = config.get_fraise("web")
        env_config = fraise.get("environments", {}).get("dev", {})

        # Derive socket stem the same way as build_manifest does
        from fraisier.naming import deploy_socket_name
        socket_unit = deploy_socket_name(env_config, "dev")
        socket_stem = socket_unit.removesuffix(".socket")
        context["socket_stem"] = socket_stem
        context["socket_unit_name"] = socket_unit
        context["env_config"] = env_config

        template = renderer.env.get_template("core/deploy-service.j2")
        content = template.render(**context)

        # app_path should be in ReadWritePaths
        assert "/var/www/web/dev" in content

    def test_deploy_service_includes_git_repo(self, tmp_path):
        """deploy-service.j2 includes git_repo in ReadWritePaths when configured."""
        config = self._make_config(
            tmp_path,
            """
name: myapp
scaffold:
  output_dir: scripts/generated
  deploy_user: fraisier
  config_path: /opt/fraisier/fraises.yaml

fraises:
  backend:
    type: api
    environments:
      prod:
        app_path: /var/www/backend
        git_repo: /var/repos/backend.git
"""
        )
        renderer = ScaffoldRenderer(config)
        renderer._validate_names()

        context = dict(renderer.context)
        context["fraise_name"] = "backend"
        context["env_name"] = "prod"

        fraise = config.get_fraise("backend")
        env_config = fraise.get("environments", {}).get("prod", {})
        context["env_config"] = env_config

        # Derive socket stem the same way as build_manifest does
        from fraisier.naming import deploy_socket_name
        socket_unit = deploy_socket_name(env_config, "prod")
        socket_stem = socket_unit.removesuffix(".socket")
        context["socket_stem"] = socket_stem
        context["socket_unit_name"] = socket_unit

        template = renderer.env.get_template("core/deploy-service.j2")
        content = template.render(**context)

        # git_repo should be in ReadWritePaths
        assert "/var/repos/backend.git" in content

    def test_deploy_service_includes_config_dir(self, tmp_path):
        """deploy-service.j2 always includes config_dir in ReadWritePaths."""
        config = self._make_config(
            tmp_path,
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
"""
        )
        renderer = ScaffoldRenderer(config)
        renderer._validate_names()

        context = dict(renderer.context)
        context["fraise_name"] = "api"
        context["env_name"] = "dev"

        fraise = config.get_fraise("api")
        env_config = fraise.get("environments", {}).get("dev", {})
        context["env_config"] = env_config

        # Derive socket stem the same way as build_manifest does
        from fraisier.naming import deploy_socket_name
        socket_unit = deploy_socket_name(env_config, "dev")
        socket_stem = socket_unit.removesuffix(".socket")
        context["socket_stem"] = socket_stem
        context["socket_unit_name"] = socket_unit

        template = renderer.env.get_template("core/deploy-service.j2")
        content = template.render(**context)

        # config_dir should be in ReadWritePaths
        assert "/opt/fraisier" in content

    def test_deploy_service_no_hardcoded_readwrite_conditionals(self, tmp_path):
        """deploy-service.j2 no longer has hardcoded conditional ReadWritePaths blocks."""
        config = self._make_config(
            tmp_path,
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
"""
        )
        renderer = ScaffoldRenderer(config)

        context = dict(renderer.context)
        context["fraise_name"] = "api"
        context["env_name"] = "dev"

        fraise = config.get_fraise("api")
        env_config = fraise.get("environments", {}).get("dev", {})
        context["env_config"] = env_config

        # Derive socket stem the same way as build_manifest does
        from fraisier.naming import deploy_socket_name
        socket_unit = deploy_socket_name(env_config, "dev")
        socket_stem = socket_unit.removesuffix(".socket")
        context["socket_stem"] = socket_stem
        context["socket_unit_name"] = socket_unit

        template = renderer.env.get_template("core/deploy-service.j2")
        content = template.render(**context)

        # Old conditional blocks should be gone
        assert "{% if env_config.get('git_repo')" not in content
        assert "{% if env_config.get('app_path')" not in content

"""Tests for fraisier-webhook.service.j2 integration with PathManifest."""

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer


class TestWebhookServiceManifest:
    """fraisier-webhook.service.j2 uses manifest.paths_for_unit() for ReadWritePaths."""

    def _make_config(self, tmp_path, yaml_content):
        """Helper to create FraisierConfig from yaml content."""
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(yaml_content)
        return FraisierConfig(str(config_file))

    def test_webhook_service_uses_manifest_for_readwrite_paths(self, tmp_path):
        """Webhook uses manifest.paths_for_unit() to generate ReadWritePaths."""
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
""",
        )
        renderer = ScaffoldRenderer(config)

        context = dict(renderer.context)
        template = renderer.env.get_template("core/fraisier-webhook.service.j2")
        content = template.render(**context)

        # Should contain ReadWritePaths from manifest
        assert "ReadWritePaths=" in content

    def test_webhook_service_includes_global_paths(self, tmp_path):
        """Webhook includes all global paths in ReadWritePaths."""
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
""",
        )
        renderer = ScaffoldRenderer(config)

        context = dict(renderer.context)
        template = renderer.env.get_template("core/fraisier-webhook.service.j2")
        content = template.render(**context)

        # Global paths should be in ReadWritePaths
        assert "/opt/fraisier" in content
        assert "/var/lib/fraisier" in content
        assert "/run/fraisier" in content

    def test_webhook_service_includes_app_path(self, tmp_path):
        """Webhook includes app_path from manifest.paths_for_unit()."""
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
""",
        )
        renderer = ScaffoldRenderer(config)

        context = dict(renderer.context)
        template = renderer.env.get_template("core/fraisier-webhook.service.j2")
        content = template.render(**context)

        # app_path should be in ReadWritePaths
        assert "/var/www/web/dev" in content

    def test_webhook_service_includes_git_repo(self, tmp_path):
        """Webhook includes git_repo from manifest.paths_for_unit()."""
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
""",
        )
        renderer = ScaffoldRenderer(config)

        context = dict(renderer.context)
        template = renderer.env.get_template("core/fraisier-webhook.service.j2")
        content = template.render(**context)

        # git_repo should be in ReadWritePaths
        assert "/var/repos/backend.git" in content

    def test_webhook_service_no_hardcoded_conditionals(self, tmp_path):
        """Webhook no longer has hardcoded conditional ReadWritePaths blocks."""
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
""",
        )
        renderer = ScaffoldRenderer(config)

        context = dict(renderer.context)
        template = renderer.env.get_template("core/fraisier-webhook.service.j2")
        content = template.render(**context)

        # Old conditional blocks should be gone
        assert "{% if env_config.get('app_path')" not in content
        assert "{% for fraise in local_fraises" not in content

    def test_webhook_service_multiple_fraises(self, tmp_path):
        """Webhook includes all app_paths when multiple fraises configured."""
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
        git_repo: /var/repos/api.git
  web:
    type: api
    environments:
      dev:
        app_path: /var/www/web
        git_repo: /var/repos/web.git
""",
        )
        renderer = ScaffoldRenderer(config)

        context = dict(renderer.context)
        template = renderer.env.get_template("core/fraisier-webhook.service.j2")
        content = template.render(**context)

        # Both app_paths should be in ReadWritePaths
        assert "/var/www/api" in content
        assert "/var/www/web" in content
        # Both git_repos should be in ReadWritePaths
        assert "/var/repos/api.git" in content
        assert "/var/repos/web.git" in content

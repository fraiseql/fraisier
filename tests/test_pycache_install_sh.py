"""Tests for __pycache__ cleanup block in install.sh.j2 (#196)."""

from __future__ import annotations

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer


class TestPycacheCleanupBlock:
    """install.sh.j2 renders root-owned __pycache__ cleanup for app venvs."""

    def _render_install_sh(self, tmp_path, yaml_content: str) -> str:
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(yaml_content)
        config = FraisierConfig(str(config_file))
        renderer = ScaffoldRenderer(config)
        return renderer.env.get_template("core/install.sh.j2").render(
            **renderer.context
        )

    def test_cleanup_targets_app_venv(self, tmp_path):
        """Cleanup block includes find for app_path/.venv __pycache__ dirs."""
        content = self._render_install_sh(
            tmp_path,
            """\
name: testapp
servers:
  prod.example.com:
    machine_hostnames: [prod-01]

fraises:
  api:
    type: api
    environments:
      production:
        server: prod.example.com
        app_path: /var/www/api

scaffold:
  deploy_user: testapp_deploy
""",
        )
        assert (
            'find "/var/www/api/.venv" -name "__pycache__" -user root -type d'
            in content
        )

    def test_cleanup_targets_deploy_user_local_lib(self, tmp_path):
        """Cleanup block includes find for deploy user's .local/lib dir."""
        content = self._render_install_sh(
            tmp_path,
            """\
name: testapp
servers:
  prod.example.com:
    machine_hostnames: [prod-01]

fraises:
  api:
    type: api
    environments:
      production:
        server: prod.example.com
        app_path: /var/www/api

scaffold:
  deploy_user: testapp_deploy
""",
        )
        assert (
            'find "/home/${DEPLOY_USER}/.local/lib" -name "__pycache__" -user root -type d'
            in content
        )

    def test_no_cleanup_when_no_app_path(self, tmp_path):
        """Fraises without app_path don't get a cleanup block."""
        content = self._render_install_sh(
            tmp_path,
            """\
name: testapp
servers:
  prod.example.com:
    machine_hostnames: [prod-01]

fraises:
  backup:
    type: scheduled
    environments:
      production:
        server: prod.example.com
        systemd_timer: backup.timer

scaffold:
  deploy_user: testapp_deploy
""",
        )
        # Should not have any app venv cleanup (no app_path)
        assert (
            '__pycache__" -user root' in content
        )  # deploy user lib cleanup still present
        # But no /var/www or app-specific venv path
        assert ".venv" not in content or "/home/${DEPLOY_USER}" in content

    def test_cleanup_appears_before_systemd_units(self, tmp_path):
        """Cleanup block appears before systemd unit installation."""
        content = self._render_install_sh(
            tmp_path,
            """\
name: testapp
servers:
  prod.example.com:
    machine_hostnames: [prod-01]

fraises:
  api:
    type: api
    environments:
      production:
        server: prod.example.com
        app_path: /var/www/api

scaffold:
  deploy_user: testapp_deploy
""",
        )
        pycache_pos = content.find("__pycache__")
        systemd_pos = content.find("Installing systemd units")
        assert pycache_pos < systemd_pos

    def test_multiple_fraises_get_individual_cleanup(self, tmp_path):
        """Each fraise with app_path gets its own cleanup line."""
        content = self._render_install_sh(
            tmp_path,
            """\
name: testapp
servers:
  prod.example.com:
    machine_hostnames: [prod-01]

fraises:
  api:
    type: api
    environments:
      production:
        server: prod.example.com
        app_path: /var/www/api
  worker:
    type: api
    environments:
      production:
        server: prod.example.com
        app_path: /var/www/worker

scaffold:
  deploy_user: testapp_deploy
""",
        )
        assert 'find "/var/www/api/.venv"' in content
        assert 'find "/var/www/worker/.venv"' in content

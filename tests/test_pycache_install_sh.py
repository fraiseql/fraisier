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
            'find "/var/www/api/.venv" -name "__pycache__" ! -user "${DEPLOY_USER}"'
            " -type d" in content
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
            'find "/home/${DEPLOY_USER}/.local/lib" -name "__pycache__"'
            ' ! -user "${DEPLOY_USER}" -type d' in content
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
            '__pycache__" ! -user "${DEPLOY_USER}"' in content
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


_UV_TOOL_YAML = """\
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
"""


class TestUvToolDirSweep:
    """The sweep must reach where uv actually installs tools (#286).

    uv tools live in ~/.local/share/uv/tools/<name>/lib/…, not ~/.local/lib, so
    the pre-existing sweeps never covered the tool venv — which is exactly where
    the reported install_user-owned __pycache__ blocks `uv tool install --force`.
    """

    def _render(self, tmp_path) -> str:
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(_UV_TOOL_YAML)
        renderer = ScaffoldRenderer(FraisierConfig(str(config_file)))
        return renderer.env.get_template("core/install.sh.j2").render(
            **renderer.context
        )

    def test_sweeps_the_uv_tool_dir(self, tmp_path):
        """The uv tool directory is swept for stray __pycache__."""
        content = self._render(tmp_path)

        assert "/home/${DEPLOY_USER}/.local/share/uv/tools" in content

    def test_tool_dir_sweep_matches_any_foreign_owner(self, tmp_path):
        """`-user root` would miss the install_user-owned residue reported."""
        content = self._render(tmp_path)

        line = next(ln for ln in content.splitlines() if ".local/share/uv/tools" in ln)
        assert '! -user "${DEPLOY_USER}"' in line
        assert "-user root" not in line

    def test_tool_dir_sweep_is_bounded_to_pycache_dirs(self, tmp_path):
        """Never a bare rm -rf on a computed path."""
        content = self._render(tmp_path)

        line = next(ln for ln in content.splitlines() if ".local/share/uv/tools" in ln)
        assert '-name "__pycache__"' in line
        assert "-type d" in line


class TestSweepsArePrivilegedAndDryRunSafe:
    """A sweep that cannot unlink another user's .pyc is a no-op.

    The deploy user cannot remove files inside an install_user-owned
    __pycache__, and the failure is swallowed by 2>/dev/null || true — so the
    sweeps only ever worked when install.sh itself ran as root.
    """

    def _sweep_lines(self, tmp_path) -> list[str]:
        config_file = tmp_path / "fraises.yaml"
        config_file.write_text(_UV_TOOL_YAML)
        renderer = ScaffoldRenderer(FraisierConfig(str(config_file)))
        content = renderer.env.get_template("core/install.sh.j2").render(
            **renderer.context
        )
        return [
            ln
            for ln in content.splitlines()
            if "__pycache__" in ln and "find" in ln and not ln.strip().startswith("#")
        ]

    def test_all_sweeps_are_privileged(self, tmp_path):
        """Every sweep runs under sudo."""
        lines = self._sweep_lines(tmp_path)

        assert len(lines) == 3, f"expected 3 sweeps, got {lines}"
        for line in lines:
            assert "sudo find" in line, f"not privileged: {line.strip()}"

    def test_all_sweeps_honour_dry_run(self, tmp_path):
        """Sweeps go through _run, so --dry-run does not delete anything."""
        lines = self._sweep_lines(tmp_path)

        for line in lines:
            assert line.strip().startswith("_run "), (
                f"bypasses _run, so --dry-run would still delete: {line.strip()}"
            )

    def test_original_sweep_targets_are_retained(self, tmp_path):
        """Widening must not drop the venv and .local/lib sweeps."""
        joined = "\n".join(self._sweep_lines(tmp_path))

        assert "/var/www/api/.venv" in joined
        assert "/home/${DEPLOY_USER}/.local/lib" in joined


_SPLIT_USER_YAML = """\
name: testapp
servers:
  prod.example.com:
    machine_hostnames: [prod-01]

fraises:
  api:
    type: api
    install:
      user: installer
      command: [/usr/bin/uv, sync, --frozen]
    environments:
      production:
        server: prod.example.com
        app_path: /var/www/api
        service:
          user: appsvc
          exec: /var/www/api/.venv/bin/uvicorn app:app
          port: 8000

scaffold:
  deploy_user: testapp_deploy
"""


def _render(tmp_path, yaml_content: str) -> str:
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(yaml_content)
    renderer = ScaffoldRenderer(FraisierConfig(str(config_file)))
    return renderer.env.get_template("core/install.sh.j2").render(**renderer.context)


def _sweep_line(content: str, needle: str) -> str:
    return next(
        ln
        for ln in content.splitlines()
        if needle in ln and "find" in ln and not ln.strip().startswith("#")
    )


class TestSweepsMatchAnyForeignOwner:
    """`-user root` misses the identity #292 is about (#303).

    `c34a6b6` widened the owner filter for the new uv-tool sweep only. The app
    venv and `.local/lib` sweeps kept `-user root`, so residue written by
    `service.user` — an identity independent of both `deploy_user` and
    `install.user` — survives every sweep. Nothing else clears it:
    `_verify_manifest_ownership` stats the venv *directory*, whose owner does
    not change when a third user writes nested `__pycache__`.
    """

    def test_app_venv_sweep_targets_the_venv_owner(self, tmp_path):
        """The venv belongs to install.user, so anything else in it is foreign."""
        line = _sweep_line(_render(tmp_path, _SPLIT_USER_YAML), "/var/www/api/.venv")

        assert '! -user "installer"' in line
        assert "-user root" not in line

    def test_app_venv_sweep_falls_back_to_deploy_user(self, tmp_path):
        """No install.user configured → the deploy user owns the venv."""
        line = _sweep_line(_render(tmp_path, _UV_TOOL_YAML), "/var/www/api/.venv")

        assert '! -user "${DEPLOY_USER}"' in line
        assert "-user root" not in line

    def test_local_lib_sweep_matches_any_foreign_owner(self, tmp_path):
        """The deploy user owns ~/.local/lib; any other owner is residue."""
        line = _sweep_line(_render(tmp_path, _SPLIT_USER_YAML), ".local/lib")

        assert '! -user "${DEPLOY_USER}"' in line
        assert "-user root" not in line

    def test_no_sweep_still_filters_on_root_alone(self, tmp_path):
        """The whole point: root is no longer the only writer we clean up after."""
        content = _render(tmp_path, _SPLIT_USER_YAML)

        sweeps = [
            ln
            for ln in content.splitlines()
            if "__pycache__" in ln and "find" in ln and not ln.strip().startswith("#")
        ]
        assert len(sweeps) == 3
        for line in sweeps:
            assert "-user root" not in line, line.strip()

    def test_sweeps_stay_bounded_and_privileged(self, tmp_path):
        """Widening the owner filter must not relax the other guards."""
        content = _render(tmp_path, _SPLIT_USER_YAML)

        sweeps = [
            ln
            for ln in content.splitlines()
            if "__pycache__" in ln and "find" in ln and not ln.strip().startswith("#")
        ]
        for line in sweeps:
            assert '-name "__pycache__"' in line
            assert "-type d" in line
            assert "sudo find" in line
            assert line.strip().startswith("_run ")

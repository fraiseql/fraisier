"""A deploy whose commit changes only a template re-renders the scaffold (#502).

The deployer is built the way the CLI, the daemon and the webhook build it,
through ``build_deployer``, which gives it no typed config. The template sync
used to read ``scaffold.template_dir`` from that typed config, so on every real
deploy it did nothing: the server kept the templates ``bootstrap`` uploaded,
the change detection hashed that frozen copy, and a template-only commit never
regenerated anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from fraisier.deployers.api import APIDeployer
from fraisier.deployers.registry import build_deployer
from fraisier.runners import LocalRunner

if TYPE_CHECKING:
    from pathlib import Path

_TEMPLATES = "scripts/scaffold-templates"
_TEMPLATE = f"{_TEMPLATES}/core/sudoers.j2"


def _checkout(tmp_path: Path, *, template_dir: str | None = _TEMPLATES) -> Path:
    """An app checkout holding fraises.yaml and one custom template."""
    app = tmp_path / "app"
    (app / _TEMPLATE).parent.mkdir(parents=True)
    (app / _TEMPLATE).write_text("# CUSTOM v1\n")
    template_line = f"  template_dir: {template_dir}\n" if template_dir else ""
    (app / "fraises.yaml").write_text(
        "name: myproj\n"
        "scaffold:\n"
        "  deploy_user: fraisier\n"
        f"{template_line}"
        "fraises:\n"
        "  my_api:\n"
        "    type: api\n"
        "    environments:\n"
        "      production:\n"
        f"        app_path: {app}\n"
    )
    return app


def _deployer(app: Path) -> APIDeployer:
    """Built as the CLI, the daemon and the webhook build it: no typed config."""
    deployer = build_deployer(
        "api",
        {"app_path": str(app), "fraise_name": "my_api", "environment": "production"},
        runner=LocalRunner(),
    )
    assert isinstance(deployer, APIDeployer)
    assert deployer.config_object is None
    return deployer


class TestTheSyncReadsTheDeployedConfig:
    def test_template_dir_reaches_the_server_config_dir(self, tmp_path):
        app = _checkout(tmp_path)
        dest = tmp_path / "opt" / "fraises.yaml"

        _deployer(app)._sync_fraises_yaml(
            source_path=app / "fraises.yaml", dest_path=dest
        )

        assert (dest.parent / _TEMPLATE).read_text() == "# CUSTOM v1\n"

    def test_nothing_is_copied_without_a_template_dir(self, tmp_path):
        app = _checkout(tmp_path, template_dir=None)
        dest = tmp_path / "opt" / "fraises.yaml"

        _deployer(app)._sync_fraises_yaml(
            source_path=app / "fraises.yaml", dest_path=dest
        )

        assert not (dest.parent / _TEMPLATES).exists()


class TestATemplateOnlyCommitRegenerates:
    """The config-sync step of a deploy, rendering and installing stubbed out."""

    @staticmethod
    def _deploy(app: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
        """Run the deploy's config sync once; return whether it regenerated."""
        deployer = _deployer(app)
        regenerated: list[Path] = []

        def regenerate(*, config_path: Path) -> None:
            regenerated.append(config_path)

        def install(*, config_path: Path) -> None:
            del config_path

        monkeypatch.setattr(deployer, "_regenerate_scaffold", regenerate)
        monkeypatch.setattr(deployer, "_install_scaffold", install)
        monkeypatch.setattr(deployer, "_refresh_config_from_synced_file", lambda: None)
        deployer._sync_config_if_needed()
        return bool(regenerated)

    @pytest.fixture
    def app(self, tmp_path, monkeypatch) -> Path:
        monkeypatch.setenv("FRAISIER_CONFIG", str(tmp_path / "opt" / "fraises.yaml"))
        return _checkout(tmp_path)

    def test_a_changed_template_regenerates(self, app, monkeypatch):
        assert self._deploy(app, monkeypatch) is True
        (app / _TEMPLATE).write_text("# CUSTOM v2\n")

        assert self._deploy(app, monkeypatch) is True

    def test_an_unchanged_commit_does_not(self, app, monkeypatch):
        assert self._deploy(app, monkeypatch) is True

        assert self._deploy(app, monkeypatch) is False


class TestAFraisierUpgradeReRenders:
    """A new fraisier re-renders the state tree, and installs nothing.

    Change detection hashes fraises.yaml and ``template_dir``, not fraisier's
    own templates, so after an upgrade the state tree kept the old version's
    render and ``scaffold-diff`` compared the host against it. The install
    stays the operator's: an upgrade must not make the next deploy stop on a
    vhost it never asked to change.
    """

    @staticmethod
    def _deploy(app: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[bool, bool]:
        """Run the deploy's config sync once; return (rendered, installed)."""
        deployer = _deployer(app)
        rendered: list[Path] = []
        installed: list[Path] = []

        def regenerate(*, config_path: Path) -> None:
            rendered.append(config_path)

        def install(*, config_path: Path) -> None:
            installed.append(config_path)

        monkeypatch.setattr(deployer, "_regenerate_scaffold", regenerate)
        monkeypatch.setattr(deployer, "_install_scaffold", install)
        monkeypatch.setattr(deployer, "_refresh_config_from_synced_file", lambda: None)
        deployer._sync_config_if_needed()
        return bool(rendered), bool(installed)

    @pytest.fixture
    def app(self, tmp_path, monkeypatch) -> Path:
        monkeypatch.setenv("FRAISIER_CONFIG", str(tmp_path / "opt" / "fraises.yaml"))
        monkeypatch.setattr("fraisier.__version__", "1.0.0")
        return _checkout(tmp_path)

    def test_an_upgrade_renders_without_installing(self, app, monkeypatch):
        assert self._deploy(app, monkeypatch) == (True, True)
        monkeypatch.setattr("fraisier.__version__", "1.1.0")

        assert self._deploy(app, monkeypatch) == (True, False)

    def test_it_renders_once_per_version(self, app, monkeypatch):
        self._deploy(app, monkeypatch)
        monkeypatch.setattr("fraisier.__version__", "1.1.0")
        self._deploy(app, monkeypatch)

        assert self._deploy(app, monkeypatch) == (False, False)

    def test_a_host_without_a_recorded_version_renders_once(self, app, monkeypatch):
        self._deploy(app, monkeypatch)
        (app.parent / "opt" / ".scaffold_rendered_by").unlink()

        assert self._deploy(app, monkeypatch) == (True, False)

    def test_a_config_change_still_installs(self, app, monkeypatch):
        self._deploy(app, monkeypatch)
        monkeypatch.setattr("fraisier.__version__", "1.1.0")
        (app / _TEMPLATE).write_text("# CUSTOM v2\n")

        assert self._deploy(app, monkeypatch) == (True, True)

    def test_a_failed_upgrade_render_neither_stops_nor_records(self, app, monkeypatch):
        self._deploy(app, monkeypatch)
        monkeypatch.setattr("fraisier.__version__", "1.1.0")
        deployer = _deployer(app)

        def broken(*, config_path: Path) -> None:
            raise RuntimeError(f"render failed for {config_path}")

        monkeypatch.setattr(deployer, "_regenerate_scaffold", broken)
        monkeypatch.setattr(deployer, "_refresh_config_from_synced_file", lambda: None)
        deployer._sync_config_if_needed()

        assert self._deploy(app, monkeypatch) == (True, False)

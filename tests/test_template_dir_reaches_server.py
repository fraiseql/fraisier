"""Custom templates must reach the server, and their absence must be loud (#312).

`scaffold.template_dir` works locally and silently does nothing on the server:

1. a relative path resolves against the *config* directory (`/opt/fraisier`),
   not the app checkout, so the directory does not exist there;
2. nothing syncs it — only `fraises.yaml` is copied;
3. `ConfigWatcher` hashes only `fraises.yaml`, so a template-only commit never
   triggers regeneration at all.

`jinja2.ChoiceLoader` falls through to the built-in template when the first
loader's directory is missing — no exception, no warning. On printoptim.dev the
customised file was `sudoers.j2`, so the operator believed a privilege rule was
deployed when it was not.
"""

from __future__ import annotations

import logging

import pytest

from fraisier.config import FraisierConfig
from fraisier.scaffold.renderer import ScaffoldRenderer

_YAML = """
name: myproj
scaffold:
  deploy_user: fraisier
  output_dir: {output}
  template_dir: {template_dir}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
"""


def _config(tmp_path, template_dir: str) -> FraisierConfig:
    p = tmp_path / "fraises.yaml"
    p.write_text(
        _YAML.format(output=str(tmp_path / "output"), template_dir=template_dir)
    )
    return FraisierConfig(p)


class TestMissingTemplateDirIsLoud:
    """The silent fallback is the dangerous part — say something."""

    def test_warns_when_configured_dir_is_absent(self, tmp_path, caplog):
        config = _config(tmp_path, "scripts/scaffold-templates")

        with caplog.at_level(logging.WARNING):
            ScaffoldRenderer(config)

        assert any(
            "scaffold-templates" in r.message and r.levelno >= logging.WARNING
            for r in caplog.records
        ), f"no warning naming the missing dir: {[r.message for r in caplog.records]}"

    def test_warning_names_the_resolved_path_not_the_configured_one(
        self, tmp_path, caplog
    ):
        """A relative path is the whole trap — show where it actually looked."""
        config = _config(tmp_path, "scripts/scaffold-templates")

        with caplog.at_level(logging.WARNING):
            ScaffoldRenderer(config)

        resolved = str(tmp_path / "scripts" / "scaffold-templates")
        assert any(resolved in r.message for r in caplog.records)

    def test_warning_says_built_ins_are_being_used(self, tmp_path, caplog):
        config = _config(tmp_path, "scripts/scaffold-templates")

        with caplog.at_level(logging.WARNING):
            ScaffoldRenderer(config)

        joined = " ".join(r.message for r in caplog.records).lower()
        assert "built-in" in joined or "builtin" in joined

    def test_silent_when_the_dir_exists(self, tmp_path, caplog):
        (tmp_path / "tpl").mkdir()
        config = _config(tmp_path, "tpl")

        with caplog.at_level(logging.WARNING):
            ScaffoldRenderer(config)

        assert not [r for r in caplog.records if "tpl" in r.message]

    def test_silent_when_no_template_dir_configured(self, tmp_path, caplog):
        p = tmp_path / "fraises.yaml"
        p.write_text(f"""
name: myproj
scaffold:
  deploy_user: fraisier
  output_dir: {tmp_path / "output"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
""")

        with caplog.at_level(logging.WARNING):
            ScaffoldRenderer(FraisierConfig(p))

        assert not [r for r in caplog.records if "template" in r.message.lower()]


class TestCustomTemplateActuallyWins:
    """Sanity: when the directory *is* present, the override is used.

    Without this the warning tests could pass against a renderer that never
    consults the custom directory at all.
    """

    def test_custom_template_overrides_the_builtin(self, tmp_path):
        tpl = tmp_path / "tpl" / "core"
        tpl.mkdir(parents=True)
        (tpl / "sudoers.j2").write_text("# CUSTOM SUDOERS MARKER\n")
        config = _config(tmp_path, "tpl")

        ScaffoldRenderer(config).render()

        assert "CUSTOM SUDOERS MARKER" in (tmp_path / "output" / "sudoers").read_text()


class TestTemplateDirIsSyncedToTheServer:
    """The templates are committed to the repo but never leave the checkout.

    Only `fraises.yaml` was copied, so the server's config directory — which
    is what a relative `template_dir` resolves against — never had them.
    """

    def _deployer(self, tmp_path, template_dir: str | None):
        from unittest.mock import MagicMock

        from fraisier.deployers.api import APIDeployer

        app = tmp_path / "app"
        (app / "scripts" / "scaffold-templates" / "core").mkdir(parents=True)
        (app / "scripts" / "scaffold-templates" / "core" / "sudoers.j2").write_text(
            "# CUSTOM\n"
        )
        cfg = f"""
name: myproj
scaffold:
  deploy_user: fraisier
{f"  template_dir: {template_dir}" if template_dir else ""}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: {app}
"""
        (app / "fraises.yaml").write_text(cfg)

        d = APIDeployer({"app_path": str(app), "fraise_name": "my_api"})
        d.runner = MagicMock()
        d.runner.run.return_value = MagicMock(returncode=0)
        return d, app

    def _synced_paths(self, runner) -> list[str]:
        """Every path that appears in a cp/rsync-ish command."""
        out = []
        for call in runner.run.call_args_list:
            out.extend(str(a) for a in call.args[0])
        return out

    def test_template_dir_is_copied_next_to_fraises_yaml(self, tmp_path):
        from fraisier.config import FraisierConfig

        d, app = self._deployer(tmp_path, "scripts/scaffold-templates")
        dest = tmp_path / "opt" / "fraises.yaml"

        with_cfg = FraisierConfig(app / "fraises.yaml")
        d.config_object = with_cfg
        d._sync_fraises_yaml(source_path=app / "fraises.yaml", dest_path=dest)

        joined = " ".join(self._synced_paths(d.runner))
        assert "scaffold-templates" in joined, (
            f"template dir never synced; commands were: {joined}"
        )

    def test_no_template_sync_when_not_configured(self, tmp_path):
        from fraisier.config import FraisierConfig

        d, app = self._deployer(tmp_path, None)
        dest = tmp_path / "opt" / "fraises.yaml"
        d.config_object = FraisierConfig(app / "fraises.yaml")

        d._sync_fraises_yaml(source_path=app / "fraises.yaml", dest_path=dest)

        assert "scaffold-templates" not in " ".join(self._synced_paths(d.runner))


class TestTemplateOnlyChangeTriggersRegen:
    """A commit touching only a template must flip has_changed() (#312).

    ConfigWatcher hashed fraises.yaml alone, so a template edit did not merely
    fail to reach the server — regeneration never even ran for it.
    """

    def _watcher(self, tmp_path, template_dir: str | None = "tpl"):
        from fraisier.config_watcher import ConfigWatcher

        (tmp_path / "fraises.yaml").write_text(
            "name: p\n"
            + (f"scaffold:\n  template_dir: {template_dir}\n" if template_dir else "")
        )
        if template_dir:
            d = tmp_path / template_dir / "core"
            d.mkdir(parents=True)
            (d / "sudoers.j2").write_text("v1\n")
        return ConfigWatcher(tmp_path)

    def test_editing_a_template_changes_the_hash(self, tmp_path):
        w = self._watcher(tmp_path)
        before = w.compute_hash()

        (tmp_path / "tpl" / "core" / "sudoers.j2").write_text("v2\n")

        assert w.compute_hash() != before

    def test_adding_a_template_changes_the_hash(self, tmp_path):
        w = self._watcher(tmp_path)
        before = w.compute_hash()

        (tmp_path / "tpl" / "core" / "service.j2").write_text("new\n")

        assert w.compute_hash() != before

    def test_renaming_a_template_changes_the_hash(self, tmp_path):
        """Content alone is not enough — the path is part of the identity."""
        w = self._watcher(tmp_path)
        before = w.compute_hash()

        core = tmp_path / "tpl" / "core"
        (core / "sudoers.j2").rename(core / "sudoers-renamed.j2")

        assert w.compute_hash() != before

    def test_hash_is_stable_when_nothing_changes(self, tmp_path):
        w = self._watcher(tmp_path)

        assert w.compute_hash() == w.compute_hash()

    def test_unchanged_behaviour_without_a_template_dir(self, tmp_path):
        w = self._watcher(tmp_path, template_dir=None)
        before = w.compute_hash()

        (tmp_path / "fraises.yaml").write_text("name: p\n# edited\n")

        assert w.compute_hash() != before


class TestTheChainComposes:
    """Sync + resolution together must actually deliver the customisation.

    Each piece alone leaves the bug in place: syncing without regen never runs,
    and warning without syncing only announces the problem. This walks the real
    sequence — checkout → sync → render from the *config* directory — and
    asserts the custom rule lands.
    """

    def test_custom_sudoers_survives_the_trip_to_the_server(self, tmp_path):
        import shutil

        from fraisier.config import FraisierConfig
        from fraisier.config_watcher import ConfigWatcher

        # 1. the app checkout, with a customised template committed alongside
        app = tmp_path / "app"
        (app / "scripts" / "scaffold-templates" / "core").mkdir(parents=True)
        (app / "scripts" / "scaffold-templates" / "core" / "sudoers.j2").write_text(
            "# CUSTOM RULE FROM THE REPO\n"
        )
        (app / "fraises.yaml").write_text(f"""
name: myproj
scaffold:
  deploy_user: fraisier
  template_dir: scripts/scaffold-templates
  output_dir: {tmp_path / "out"}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: {app}
""")

        # 2. the server's config dir — what a relative template_dir resolves against
        opt = tmp_path / "opt"
        opt.mkdir()
        shutil.copy(app / "fraises.yaml", opt / "fraises.yaml")

        # before the sync: built-in wins, silently
        ScaffoldRenderer(FraisierConfig(opt / "fraises.yaml")).render()
        assert "CUSTOM RULE" not in (tmp_path / "out" / "sudoers").read_text()

        # 3. the sync this fix adds
        shutil.copytree(
            app / "scripts" / "scaffold-templates",
            opt / "scripts" / "scaffold-templates",
        )

        # after: the repo's rule is what renders on the server
        ScaffoldRenderer(FraisierConfig(opt / "fraises.yaml")).render()
        assert "CUSTOM RULE FROM THE REPO" in (tmp_path / "out" / "sudoers").read_text()

        # 4. and a template-only edit is now visible to change detection
        watcher = ConfigWatcher(opt)
        before = watcher.compute_hash()
        (opt / "scripts" / "scaffold-templates" / "core" / "sudoers.j2").write_text(
            "# EDITED\n"
        )
        assert watcher.compute_hash() != before

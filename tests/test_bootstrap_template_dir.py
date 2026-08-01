"""Bootstrap must upload scaffold.template_dir too (#318, follow-up to #312).

#312 made deploy-time config sync carry the template tree to the server.
`bootstrap._upload_config` still uploaded exactly one file, so a freshly
bootstrapped host had a `fraises.yaml` whose `template_dir` pointed at a
directory that was not there.

Bounded, not catastrophic: bootstrap renders its initial scaffold *locally*
(where the path resolves), never renders server-side, and the first deploy
self-heals it. The exposure is the window in between, where any server-side
render silently produces built-ins.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fraisier.bootstrap import ServerBootstrapper
from fraisier.config import FraisierConfig


def _make(
    tmp_path, template_dir: str | None, *, create: bool = True, dry: bool = False
):
    if create and template_dir:
        d = tmp_path / template_dir / "core"
        d.mkdir(parents=True)
        (d / "sudoers.j2").write_text("# CUSTOM\n")

    cfg_path = tmp_path / "fraises.yaml"
    cfg_path.write_text(f"""
name: myapp
scaffold:
  deploy_user: fraisier
{f"  template_dir: {template_dir}" if template_dir else ""}
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /var/www/api
""")
    runner = MagicMock()
    runner.run.return_value = MagicMock(returncode=0)
    boot = ServerBootstrapper(
        config=FraisierConfig(cfg_path),
        environment="production",
        runner=runner,
        fraises_yaml_path=cfg_path,
        dry_run=dry,
    )
    return boot, runner


def _all_args(runner) -> str:
    parts = []
    for call in runner.run.call_args_list:
        parts.extend(str(a) for a in call.args[0])
    for call in runner.upload.call_args_list:
        parts.extend(str(a) for a in call.args)
    for call in runner.upload_tree.call_args_list:
        parts.extend(str(a) for a in call.args)
    return " ".join(parts)


class TestBootstrapUploadsTemplateDir:
    def test_template_tree_is_uploaded(self, tmp_path):
        boot, runner = _make(tmp_path, "scripts/scaffold-templates")

        step = boot._upload_config()

        assert step.success is True
        assert "scaffold-templates" in _all_args(runner), (
            f"template dir never uploaded: {_all_args(runner)}"
        )

    def test_destination_is_under_the_server_config_dir(self, tmp_path):
        """A relative template_dir resolves against /opt/fraisier on the server."""
        boot, runner = _make(tmp_path, "scripts/scaffold-templates")

        boot._upload_config()

        assert "/opt/fraisier/scripts/scaffold-templates" in _all_args(runner)

    def test_config_upload_still_happens(self, tmp_path):
        """The template sync must be additive, never replace the config upload."""
        boot, runner = _make(tmp_path, "scripts/scaffold-templates")

        boot._upload_config()

        assert runner.upload.call_args_list[0].args[1] == "/opt/fraisier/fraises.yaml"

    def test_stale_templates_are_cleared_first(self, tmp_path):
        """A template deleted upstream must not survive and shadow a built-in."""
        boot, runner = _make(tmp_path, "scripts/scaffold-templates")

        boot._upload_config()

        assert "rm -rf /opt/fraisier/scripts/scaffold-templates" in _all_args(runner)


class TestBootstrapTemplateDirEdgeCases:
    def test_no_upload_when_not_configured(self, tmp_path):
        boot, runner = _make(tmp_path, None)

        boot._upload_config()

        assert "scaffold-templates" not in _all_args(runner)

    def test_absolute_template_dir_is_not_uploaded(self, tmp_path):
        """An absolute path names a location the operator manages on the server."""
        boot, runner = _make(tmp_path, str(tmp_path / "abs-templates"))

        boot._upload_config()

        assert "abs-templates" not in _all_args(runner)

    def test_missing_directory_does_not_fail_bootstrap(self, tmp_path):
        """Configured but absent locally: warn, do not abort provisioning."""
        boot, _runner = _make(tmp_path, "scripts/scaffold-templates", create=False)

        step = boot._upload_config()

        assert step.success is True

    def test_dry_run_uploads_nothing(self, tmp_path):
        boot, runner = _make(tmp_path, "scripts/scaffold-templates", dry=True)

        step = boot._upload_config()

        assert step.success is True
        runner.upload.assert_not_called()
        runner.upload_tree.assert_not_called()
        runner.run.assert_not_called()

    def test_dry_run_plan_mentions_the_template_dir(self, tmp_path):
        """--dry-run is the review surface; a silent extra upload defeats it."""
        boot, _ = _make(tmp_path, "scripts/scaffold-templates", dry=True)

        step = boot._upload_config()

        assert "scaffold-templates" in (step.command or "")

    def test_upload_failure_is_not_fatal(self, tmp_path):
        """Best-effort, matching the deploy path: log loudly, keep provisioning."""
        boot, runner = _make(tmp_path, "scripts/scaffold-templates")
        runner.upload_tree.side_effect = subprocess.CalledProcessError(1, "scp")

        step = boot._upload_config()

        assert step.success is True

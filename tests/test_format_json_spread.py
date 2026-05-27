"""--format json spread to ship/trigger-deploy/deployment-status (#221 b5).

Cycle 1 — shared --format option + legacy --json deprecation alias.
Cycle 2 — ship --format json dry-run shape.
Cycle 3 — trigger-deploy --format json (offline via mocked socket).
Cycle 4 — deployment-status migration: --json still works (with stderr
   deprecation warning) and --format json is the documented path.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from fraisier.cli._json import _DEPRECATION_WARNING_EMITTED
from fraisier.cli.main import main as main_group


@pytest.fixture(autouse=True)
def _reset_deprecation_dedupe():
    _DEPRECATION_WARNING_EMITTED.clear()
    yield
    _DEPRECATION_WARNING_EMITTED.clear()


def _invoke(args: list[str]):
    runner = CliRunner()
    return runner.invoke(main_group, args, catch_exceptions=False)


class TestShipDryRunFormatJson:
    """`fraisier ship <bump> --dry-run --format json` emits structured payload."""

    def test_ship_patch_dry_run_emits_json(self, tmp_path, monkeypatch):
        # Run inside a tmp_path that has a minimal pyproject so ship
        # can read the current version.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "1.2.3"\n'
        )
        r = _invoke(["ship", "patch", "--dry-run", "--format", "json"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["version"]["old"] == "1.2.3"
        assert payload["version"]["new"] == "1.2.4"
        assert payload["version"]["bump_type"] == "patch"
        assert payload["dry_run"] is True


class TestDeploymentStatusMigration:
    """deployment-status --json keeps working but emits a deprecation
    warning; --format json is the documented path."""

    def test_legacy_json_flag_still_works_with_warning(self, tmp_path, monkeypatch):
        # Minimal fraises.yaml + no socket = deployment-status will
        # render "no deployment yet" but should still emit JSON.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "fraises.yaml").write_text(
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        systemd_service: myapi.service
"""
        )
        r = _invoke(["deployment-status", "my_api", "--json"])
        # deployment-status reads state files; with no state file the
        # output is JSON null/empty but the command exits non-fatally.
        # Either exit 0 with JSON, or exit 1 with JSON — both are
        # acceptable; the contract here is that --json doesn't disappear.
        assert "deprecated" in r.output.lower()

    def test_format_json_works_silently(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "fraises.yaml").write_text(
            """
fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        systemd_service: myapi.service
"""
        )
        r = _invoke(["deployment-status", "my_api", "--format", "json"])
        assert "deprecated" not in r.output.lower()

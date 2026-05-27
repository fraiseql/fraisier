"""Each subcommand's --help epilog gains a "Reads envvars:" section
derived from the introspection map (#221 bundle B phase 02).
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main as main_group


@pytest.fixture
def fixture_config(tmp_path, monkeypatch):
    """Write a small fraises.yaml with one !envvar ref per major section."""
    config_file = tmp_path / "fraises.yaml"
    config_file.write_text(
        """
git:
  provider: github
  github:
    webhook_secret: !envvar GH_TOKEN

ship:
  pr_base: !envvar SHIP_PR_BASE

fraises:
  my_api:
    type: api
    environments:
      production:
        app_path: /srv/myapi
        systemd_service: myapi.service
        database:
          database_url: !envvar DB_URL
        smoke_tests:
          - name: auth
            url: /me
            headers:
              Authorization: !envvar SMOKE_JWT
            assert: []
"""
    )
    monkeypatch.chdir(tmp_path)
    return config_file


def _help_for(args: list[str]) -> str:
    runner = CliRunner()
    result = runner.invoke(main_group, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result.output


def test_ship_help_lists_envvars_from_config_explicit_config_flag(fixture_config):
    """Pass --config explicitly at group level."""
    out = _help_for(["--config", str(fixture_config), "ship", "--help"])
    assert "Reads envvars:" in out
    assert "GH_TOKEN" in out
    assert "SHIP_PR_BASE" in out


def test_ship_help_lists_envvars_from_config_no_explicit_flag(fixture_config):
    """No --config; CWD discovery via the chdir in the fixture."""
    out = _help_for(["ship", "--help"])
    assert "Reads envvars:" in out
    assert "GH_TOKEN" in out


def test_envvar_listing_marks_unset(fixture_config, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    out = _help_for(["--config", str(fixture_config), "ship", "--help"])
    # Unset markers should appear next to the env var name.
    assert "GH_TOKEN" in out
    assert "[unset]" in out


def test_envvar_listing_marks_set(fixture_config, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "value")
    monkeypatch.setenv("SHIP_PR_BASE", "main")
    out = _help_for(["--config", str(fixture_config), "ship", "--help"])
    # When set, no [unset] tag next to GH_TOKEN's entry.
    # Simpler assertion: with both set, [unset] should not appear at all.
    assert "[unset]" not in out


def test_help_works_without_fraises_yaml(tmp_path, monkeypatch):
    """No fraises.yaml in CWD and no --config — must not raise."""
    monkeypatch.chdir(tmp_path)
    out = _help_for(["ship", "--help"])
    # Help body intact; placeholder appears (or section omitted gracefully).
    assert "Bump version" in out  # part of ship's docstring


def test_help_omitted_for_commands_without_config_access(fixture_config):
    """`version show` reads version.json, not fraises.yaml — no envvar section."""
    out = _help_for(["--config", str(fixture_config), "version", "show", "--help"])
    assert "Reads envvars:" not in out


def test_deploy_help_lists_db_url_when_config_loaded(fixture_config):
    out = _help_for(
        [
            "--config",
            str(fixture_config),
            "trigger-deploy",
            "--help",
        ]
    )
    assert "Reads envvars:" in out
    assert "DB_URL" in out
    assert "SMOKE_JWT" in out

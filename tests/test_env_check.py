"""`fraisier env-check <subcommand>` CLI tests (#221 bundle B phase 03).

Standalone preflight: report which env vars a subcommand would need
and which are currently unset. Designed for CI pipelines — "check
before invoking" instead of "invoke and learn from the error."
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main as main_group

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fixture_config(tmp_path) -> Path:
    config = tmp_path / "fraises.yaml"
    config.write_text(
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
      staging:
        app_path: /srv/myapi-stg
        systemd_service: myapi-stg.service
        database:
          database_url: !envvar STG_DB_URL
"""
    )
    return config


def _invoke(args: list[str]):
    runner = CliRunner()
    return runner.invoke(main_group, args, catch_exceptions=False)


def test_unknown_subcommand_errors(fixture_config):
    r = _invoke(["--config", str(fixture_config), "env-check", "banana"])
    assert r.exit_code != 0
    # Click's error path or our own — either way, the unknown name must
    # be surfaced.
    assert "banana" in r.output.lower() or "unknown" in r.output.lower()


def test_ship_renders_table(fixture_config, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("SHIP_PR_BASE", "main")
    r = _invoke(["--config", str(fixture_config), "env-check", "ship"])
    assert r.exit_code == 0
    assert "GH_TOKEN" in r.output
    assert "SHIP_PR_BASE" in r.output


def test_exit_code_zero_when_all_set(fixture_config, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("SHIP_PR_BASE", "main")
    r = _invoke(["--config", str(fixture_config), "env-check", "ship"])
    assert r.exit_code == 0


def test_exit_code_nonzero_when_any_unset(fixture_config, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("SHIP_PR_BASE", raising=False)
    r = _invoke(["--config", str(fixture_config), "env-check", "ship"])
    assert r.exit_code == 1


def test_required_only_lists_only_unset(fixture_config, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.delenv("SHIP_PR_BASE", raising=False)
    r = _invoke(
        ["--config", str(fixture_config), "env-check", "ship", "--required-only"]
    )
    assert "SHIP_PR_BASE" in r.output
    # GH_TOKEN is set, so --required-only filters it out.
    assert "GH_TOKEN" not in r.output


def test_json_output_schema(fixture_config, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.delenv("SHIP_PR_BASE", raising=False)
    r = _invoke(
        ["--config", str(fixture_config), "env-check", "ship", "--format", "json"]
    )
    # exit 1 because one var is unset; output is still parseable JSON
    assert r.exit_code == 1
    payload = json.loads(r.output)
    assert payload["subcommand"] == "ship"
    assert isinstance(payload["envvars"], list)
    assert payload["all_set"] is False
    assert payload["unset_count"] >= 1
    for ref in payload["envvars"]:
        assert {"name", "yaml_path", "is_set"} <= set(ref.keys())


def test_fraise_environment_narrows_scope(fixture_config, monkeypatch):
    # With --fraise my_api --environment staging, the production-only
    # DB_URL should not appear.
    monkeypatch.setenv("STG_DB_URL", "stg")
    monkeypatch.delenv("DB_URL", raising=False)
    r = _invoke(
        [
            "--config",
            str(fixture_config),
            "env-check",
            "trigger-deploy",
            "--fraise",
            "my_api",
            "--environment",
            "staging",
            "--format",
            "json",
        ]
    )
    assert r.exit_code == 0
    payload = json.loads(r.output)
    names = [r_["name"] for r_ in payload["envvars"]]
    assert "STG_DB_URL" in names
    assert "DB_URL" not in names

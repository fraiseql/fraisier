"""CLI regression for issue #220 — unset !envvar must not block --help.

The headline reproduction from the issue: a fraise has
``smoke_tests[].token_provider.client_secret: !envvar SMOKE_CLIENT_SECRET``
and the operator runs ``fraisier ship --help`` (or anything that does
NOT consume that section). The CLI must not require ``SMOKE_CLIENT_SECRET``
to be set in the environment.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from fraisier.cli.main import main

_CONFIG_WITH_SMOKE_SECRET = """
git:
  provider: github
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
        health_check:
          url: https://api.example.com/health
        smoke_tests:
          - name: graphql_authed
            url: /graphql
            method: POST
            on_failure: rollback
            token_provider:
              type: oauth2_client_credentials
              token_url: https://idp.example.com/token
              client_id: my-client
              client_secret: !envvar SMOKE_CLIENT_SECRET
"""

# A config that references !envvar from every section the loader visits:
# fraises (smoke_tests + token_provider), notifications, and webhook.
# Reproduces the "subcommands that don't read these sections still demand
# every env tag" footgun #220 set out to fix.
_CONFIG_WITH_ENVVARS_EVERYWHERE = """
git:
  provider: github
webhook:
  github_secret: !envvar GITHUB_WEBHOOK_SECRET
notifications:
  slack:
    webhook_url: !envvar SLACK_WEBHOOK_URL
fraises:
  api:
    type: api
    environments:
      prod:
        app_path: /tmp/api
        health_check:
          url: https://api.example.com/health
        smoke_tests:
          - name: graphql_authed
            url: /graphql
            method: POST
            on_failure: rollback
            headers:
              Authorization: !envvar SMOKE_TEST_JWT
            token_provider:
              type: oauth2_client_credentials
              token_url: https://idp.example.com/token
              client_id: my-client
              client_secret: !envvar SMOKE_CLIENT_SECRET
"""


@pytest.fixture
def _delenv_all(monkeypatch):
    """Clear every env tag referenced by the configs in this module."""
    for var in (
        "SMOKE_CLIENT_SECRET",
        "SMOKE_TEST_JWT",
        "SLACK_WEBHOOK_URL",
        "GITHUB_WEBHOOK_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)


def _write(tmp_path, content):
    path = tmp_path / "fraises.yaml"
    path.write_text(content)
    return path


def test_ship_help_with_unset_smoke_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("SMOKE_CLIENT_SECRET", raising=False)
    cfg = _write(tmp_path, _CONFIG_WITH_SMOKE_SECRET)

    runner = CliRunner()
    result = runner.invoke(main, ["-c", str(cfg), "ship", "--help"])

    assert result.exit_code == 0, (
        "ship --help must not require SMOKE_CLIENT_SECRET; got "
        f"exit={result.exit_code}\nOutput:\n{result.output}"
    )


def test_list_with_unset_smoke_secret(tmp_path, monkeypatch):
    # `fraisier list` enumerates fraises (touches the fraises mapping
    # but not get_fraise_environment for each), so it should also be
    # safe with !envvar refs unset.
    monkeypatch.delenv("SMOKE_CLIENT_SECRET", raising=False)
    cfg = _write(tmp_path, _CONFIG_WITH_SMOKE_SECRET)

    runner = CliRunner()
    result = runner.invoke(main, ["-c", str(cfg), "list"])

    assert result.exit_code == 0, (
        f"list must not require SMOKE_CLIENT_SECRET; got exit={result.exit_code}"
        f"\nOutput:\n{result.output}"
    )


def test_validate_does_not_complain_about_unset_envvar(tmp_path, monkeypatch):
    # Issue-220 policy (Phase 1 + Phase 3): `validate` walks every
    # section but does NOT resolve env vars. The output may flag other
    # unrelated environment-state issues (e.g. missing system users)
    # — what matters is that the SMOKE_CLIENT_SECRET !envvar reference
    # is NOT surfaced as a config error. Phase 6 layers a
    # --resolve-envvars opt-in for the eager-fail workflow.
    monkeypatch.delenv("SMOKE_CLIENT_SECRET", raising=False)
    cfg = _write(tmp_path, _CONFIG_WITH_SMOKE_SECRET)

    runner = CliRunner()
    result = runner.invoke(main, ["-c", str(cfg), "validate"])

    assert "SMOKE_CLIENT_SECRET" not in result.output, (
        f"validate leaked envvar resolution into its output:\n{result.output}"
    )


# Cycle 6.1 — `--help` / `--version` with !envvar references in every
# section. These paths never read a config section, so they MUST exit 0
# regardless of env state. A failure here points at Phase 1 (Stage 1
# doing too much).


def test_help_with_unset_envvars_everywhere(tmp_path, _delenv_all):
    cfg = _write(tmp_path, _CONFIG_WITH_ENVVARS_EVERYWHERE)

    runner = CliRunner()
    result = runner.invoke(main, ["-c", str(cfg), "--help"])

    assert result.exit_code == 0, (
        "fraisier --help must not require any !envvar; got "
        f"exit={result.exit_code}\nOutput:\n{result.output}"
    )


def test_version_with_unset_envvars_everywhere(tmp_path, _delenv_all):
    cfg = _write(tmp_path, _CONFIG_WITH_ENVVARS_EVERYWHERE)

    runner = CliRunner()
    result = runner.invoke(main, ["-c", str(cfg), "--version"])

    assert result.exit_code == 0, (
        "fraisier --version must not require any !envvar; got "
        f"exit={result.exit_code}\nOutput:\n{result.output}"
    )

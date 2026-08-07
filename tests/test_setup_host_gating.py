"""``fraisier setup`` provisions this host's environments, or refuses (#331).

``_resolve_allowed_scopes`` used to answer "which environments am I?"
by matching the machine's hostname against *logical server names* only. A
config that registers its machines under ``servers:.machine_hostnames`` — the
map that exists precisely because a logical server is not a machine hostname —
matched nothing, and no match meant ``None``, which the caller reads as
*provision every environment*.

That is not a benign default. ``setup`` creates users, chowns trees, installs
and **enables** systemd units and nginx vhosts; doing all of it for every
environment on a box that could not identify itself is a refusal to answer
combined with maximum action. It is also a live candidate for how a prod-only
host acquires dev and staging units — the #325 failure shape, one level up.

So the resolution routes through ``resolve_local_server`` (the sole host
authority since v0.56.0) and fails closed. The genuine single-host case is a
separate branch, not a fallback: when no environment declares a ``server:``,
"every environment" and "this host's environments" are the same set.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from fraisier.config import FraisierConfig
from fraisier.errors import ValidationError
from fraisier.setup import ServerSetup

# The asymmetric multi-host shape: one host carries two environments, the
# other carries one. Registered by machine hostname, the way #331 reports it.
MACHINE_REGISTERED_CONFIG = """\
name: tp
fraises:
  my_api:
    type: api
    description: Test API
    environments:
      development:
        app_path: /var/www/my-api-dev
        systemd_service: my-api-dev.service
        git_repo: /var/git/my-api-dev.git
      staging:
        app_path: /var/www/my-api-stg
        systemd_service: my-api-stg.service
        git_repo: /var/git/my-api-stg.git
      production:
        app_path: /var/www/my-api
        systemd_service: my-api.service
        git_repo: /var/git/my-api.git

servers:
  dev.example.io:
    machine_hostnames: [devbox]
  prod.example.io:
    machine_hostnames: [pio]

environments:
  development:
    server: dev.example.io
  staging:
    server: dev.example.io
  production:
    server: prod.example.io
"""

SINGLE_HOST_CONFIG = """\
name: tp
fraises:
  my_api:
    type: api
    description: Test API
    environments:
      development:
        app_path: /var/www/my-api-dev
        systemd_service: my-api-dev.service
        git_repo: /var/git/my-api-dev.git
      production:
        app_path: /var/www/my-api
        systemd_service: my-api.service
        git_repo: /var/git/my-api.git
"""


def _make_config(tmp_path, yaml_content: str) -> FraisierConfig:
    p = tmp_path / "fraises.yaml"
    p.write_text(yaml_content)
    return FraisierConfig(str(p))


class _NullRunner:
    """Never invoked — these tests only build plans."""

    def run(self, *args, **kwargs):
        raise AssertionError("planning must not execute commands")


def _selected(setup, config) -> set[str] | None:
    """The environment names *setup* would provision, via its pair predicate.

    Since #336 the resolver answers by ``(fraise, environment)``; these
    tests ask the question they have always asked — which environments is
    this host — which for a single-fraise config is the same answer.
    """
    allowed = setup._resolve_allowed_scopes()
    if allowed is None:
        return None
    return {
        env_name
        for fraise_name, fraise in config.fraises.items()
        for env_name in (fraise.get("environments") or {})
        if allowed(fraise_name, env_name)
    }


def _hostnames(*names: str):
    """Patch the machine's identity as ``resolve_local_server`` reads it."""
    return patch("fraisier.scaffold.renderer.local_hostnames", return_value=list(names))


class TestMachineHostnameResolution:
    """A machine registered in ``servers:`` resolves to its logical server."""

    def test_machine_hostname_selects_only_its_environments(self, tmp_path):
        """`pio` is registered under prod.example.io — production only."""
        config = _make_config(tmp_path, MACHINE_REGISTERED_CONFIG)
        setup = ServerSetup(config, _NullRunner())

        with _hostnames("pio"):
            assert _selected(setup, config) == {"production"}

    def test_asymmetric_host_gets_both_its_environments(self, tmp_path):
        """The other side of the asymmetry: devbox carries two environments."""
        config = _make_config(tmp_path, MACHINE_REGISTERED_CONFIG)
        setup = ServerSetup(config, _NullRunner())

        with _hostnames("devbox"):
            assert _selected(setup, config) == {"development", "staging"}

    def test_logical_server_name_still_resolves(self, tmp_path):
        """Configs naming servers after their machines keep working."""
        config = _make_config(tmp_path, MACHINE_REGISTERED_CONFIG)
        setup = ServerSetup(config, _NullRunner())

        with _hostnames("prod.example.io"):
            assert _selected(setup, config) == {"production"}


class TestFailsClosed:
    """An unresolvable host is an error, never "provision everything"."""

    def test_unregistered_machine_raises(self, tmp_path):
        config = _make_config(tmp_path, MACHINE_REGISTERED_CONFIG)
        setup = ServerSetup(config, _NullRunner())

        with _hostnames("stranger"), pytest.raises(ValidationError):
            setup._resolve_allowed_scopes()

    def test_error_answers_its_own_support_question(self, tmp_path):
        """Names this machine, the known hosts, and both ways forward.

        A loud error that is copy-paste actionable is cheaper than one silent
        wrong install; this is the whole justification for skipping a
        deprecation cycle, so it is pinned rather than left to prose.
        """
        config = _make_config(tmp_path, MACHINE_REGISTERED_CONFIG)
        setup = ServerSetup(config, _NullRunner())

        with _hostnames("stranger"), pytest.raises(ValidationError) as exc:
            setup._resolve_allowed_scopes()

        message = str(exc.value)
        assert "stranger" in message
        assert "dev.example.io" in message
        assert "prod.example.io" in message
        assert "--all-environments" in message
        assert "machine_hostnames" in message

    def test_explicit_server_matching_nothing_raises(self, tmp_path):
        """`--server bogus` used to silently widen to every environment."""
        config = _make_config(tmp_path, MACHINE_REGISTERED_CONFIG)
        setup = ServerSetup(config, _NullRunner(), server="bogus")

        with pytest.raises(ValidationError) as exc:
            setup._resolve_allowed_scopes()

        assert "bogus" in str(exc.value)
        assert "prod.example.io" in str(exc.value)


class TestLegitimateWideningStillWorks:
    """Fail-closed must not break the cases that genuinely mean "everything"."""

    def test_single_host_config_provisions_everything(self, tmp_path):
        """No environment declares a server: the two sets coincide."""
        config = _make_config(tmp_path, SINGLE_HOST_CONFIG)
        setup = ServerSetup(config, _NullRunner())

        with _hostnames("anything-at-all"):
            assert _selected(setup, config) is None

    def test_all_environments_flag_is_the_escape_hatch(self, tmp_path):
        """Explicit intent to provision everything is honoured without a match."""
        config = _make_config(tmp_path, MACHINE_REGISTERED_CONFIG)
        setup = ServerSetup(config, _NullRunner(), all_environments=True)

        with _hostnames("stranger"):
            assert _selected(setup, config) is None

    def test_explicit_environment_bypasses_host_resolution(self, tmp_path):
        """--environment answers the question directly; identity is moot."""
        config = _make_config(tmp_path, MACHINE_REGISTERED_CONFIG)
        setup = ServerSetup(config, _NullRunner(), environment="staging")

        with _hostnames("stranger"):
            assert _selected(setup, config) == {"staging"}

    def test_explicit_server_selects_its_environments(self, tmp_path):
        config = _make_config(tmp_path, MACHINE_REGISTERED_CONFIG)
        setup = ServerSetup(config, _NullRunner(), server="dev.example.io")

        assert _selected(setup, config) == {"development", "staging"}


class TestCliSurfacesTheRefusal:
    """The operator reads the message on the box, not a traceback."""

    def test_unresolvable_host_exits_1_with_the_guidance(self, tmp_path):
        from click.testing import CliRunner

        from fraisier.cli.main import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(MACHINE_REGISTERED_CONFIG)

        with _hostnames("stranger"):
            result = CliRunner().invoke(
                main, ["--config", str(cfg), "setup", "--dry-run"]
            )

        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "--all-environments" in result.output
        assert "Traceback" not in result.output

    def test_registration_snippet_survives_rich_markup(self, tmp_path):
        """`machine_hostnames: [thishost]` must reach the terminal intact.

        Rich reads square brackets as style tags, so the one line an operator
        is meant to copy renders as an empty list unless markup is disabled —
        the message would look complete and be useless.
        """
        from click.testing import CliRunner

        from fraisier.cli.main import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(MACHINE_REGISTERED_CONFIG)

        with _hostnames("stranger"):
            result = CliRunner().invoke(
                main, ["--config", str(cfg), "setup", "--dry-run"]
            )

        assert "machine_hostnames: [stranger]" in result.output

    def test_mutually_exclusive_selectors_are_rejected(self, tmp_path):
        from click.testing import CliRunner

        from fraisier.cli.main import main

        cfg = tmp_path / "fraises.yaml"
        cfg.write_text(MACHINE_REGISTERED_CONFIG)

        result = CliRunner().invoke(
            main,
            ["--config", str(cfg), "setup", "--server", "x", "--all-environments"],
        )

        assert result.exit_code != 0
        assert "mutually exclusive" in result.output
